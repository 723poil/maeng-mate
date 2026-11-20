"""설정과 구체 구현을 연결하는 조립부다."""

import asyncio
from collections.abc import Callable
from dataclasses import replace

from fastapi import FastAPI

from src.application.errors import QueryReadError, SnapshotError
from src.application.mcp_access import McpSettingsService
from src.application.models import SystemStatusContext
from src.application.notion_page_links import NotionPageLinksClient
from src.application.periodic import IntervalUpdate
from src.application.ports import PeriodicStatusQuery, SnapshotReader, TodayQuery
from src.application.runtime_settings_service import RuntimeSettingsService
from src.application.source_settings import SourceSettingsService
from src.application.task_search import TaskSearch, TaskSearchQuery, TaskStatusesQuery
from src.application.today_models import MainListCard, TodayContext
from src.application.use_cases.get_my_today import get_my_today
from src.application.use_cases.get_system_status import get_system_status
from src.application.use_cases.search_my_tasks import list_my_task_statuses, search_my_tasks
from src.application.work_rules_settings import WorkRulesService
from src.application.work_settings import Settings
from src.bootstrap.calendar_wiring import build_calendar_service
from src.bootstrap.clock import now
from src.bootstrap.day_wiring import build_day_services
from src.bootstrap.environment import AppEnvironment, McpEnvironment
from src.bootstrap.mcp_wiring import build_mcp
from src.bootstrap.notification_channels import CHANNELS
from src.bootstrap.notification_wiring import build_notifications
from src.bootstrap.notion_credentials import build_notion_credentials
from src.bootstrap.notion_page_links import build_page_links
from src.bootstrap.policies import select_policy
from src.bootstrap.slack_settings import build_slack_delivery, build_slack_settings
from src.domain.daily_work import DEFAULT_WORK_RULES
from src.domain.main_list import FIELD_KINDS
from src.domain.runtime_settings import GeneralSettings, NotionSettings, RuntimeSettings
from src.infrastructure.notion.catalog import NotionCatalog
from src.infrastructure.sqlite.connection import Database
from src.infrastructure.sqlite.mcp_access import SQLiteMcpAccess
from src.infrastructure.sqlite.preferences import SQLitePreferences
from src.infrastructure.sqlite.runtime_settings import SQLiteRuntimeSettings
from src.infrastructure.sqlite.work_reader import SQLiteWorkReader
from src.infrastructure.sqlite.work_settings import SQLiteWorkSettings
from src.presentation.http import create_app


def build_api(
    environment: AppEnvironment,
    settings: Settings | None = None,
    *,
    periodic_status: PeriodicStatusQuery | None = None,
    desktop_periodic_status: PeriodicStatusQuery | None = None,
    update_interval: IntervalUpdate | None = None,
    notion_legacy_token: str | None = None,
    work_rules_service: WorkRulesService | None = None,
    restore_committed: Callable[[], bool] | None = None,
) -> FastAPI:
    """비밀값은 인증에만 전달하고 업무 조회에는 설정 여부만 주입한다."""
    context = SystemStatusContext(
        database_configured=environment.database is not None,
        notion_configured=environment.notion_token is not None,
        sources_configured=bool(settings and settings.active_sources),
        retention_confirmed=bool(settings and settings.retention_confirmed),
        relevance_policy=environment.relevance_policy,
    )

    def query_status() -> dict[str, object]:
        """업무 유스케이스를 HTTP에 필요한 호출 형태로 연결한다."""
        return get_system_status(context)

    page_links = (
        build_page_links(Database(environment.database), settings) if environment.database else None
    )
    access = SQLiteMcpAccess(Database(environment.database)) if environment.database else None
    mcp_service = (
        McpSettingsService(access, f"http://127.0.0.1:{environment.port}/mcp/") if access else None
    )
    mcp_server, mcp_query_client = (
        build_mcp(McpEnvironment(f"http://127.0.0.1:{environment.port}", environment.token))
        if access
        else (None, None)
    )

    def set_mcp_ready(ready: bool) -> None:
        """실제 SDK 시작·종료를 관리 화면의 준비 상태에 반영한다."""
        if mcp_service is not None:
            mcp_service.ready = ready

    legacy = settings.work_rules if settings else None
    day_service, work_records_service = build_day_services(
        environment,
        work_rules_service.current_rules
        if work_rules_service
        else lambda: legacy or DEFAULT_WORK_RULES,
    )
    credentials = build_notion_credentials(
        environment.notion_token,
        settings.sources if settings else (),
        legacy_token=notion_legacy_token,
    )
    notifications, groups = build_notifications(environment, settings)
    source_settings = (
        SourceSettingsService(
            SQLiteWorkSettings(
                Database(environment.database),
                notification_groups=groups,
                notification_user_id=environment.notion_user_id,
            ),
            settings,
            {"notion": NotionCatalog(lambda: credentials.selected().token)} if credentials else {},
            now=now,
            max_age_seconds=environment.snapshot_max_age_seconds,
        )
        if environment.database
        else None
    )
    work_reader = (
        SQLiteWorkReader(Database(environment.database), active_sources=settings.active_sources)
        if environment.database and settings
        else None
    )
    task_search, task_statuses = build_task_queries(environment, settings, reader=work_reader)
    return create_app(
        environment.token,
        query_status,
        mcp_service=mcp_service,
        mcp_access=access,
        mcp_server=mcp_server,
        mcp_query_client=mcp_query_client,
        on_mcp_ready=set_mcp_ready,
        restore_committed=restore_committed,
        query_today=build_today_query(
            environment, settings, reader=work_reader, page_links=page_links
        ),
        query_desktop_today=build_today_query(
            environment, settings, reader=work_reader, page_links=page_links, desktop=True
        ),
        page_links_service=page_links,
        query_tasks=task_search,
        query_task_statuses=task_statuses,
        query_periodic=periodic_status,
        query_desktop_periodic=desktop_periodic_status,
        update_interval=update_interval,
        day_service=day_service,
        calendar_service=build_calendar_service(environment, settings),
        slack_settings_service=build_slack_settings(environment),
        work_rules_service=work_rules_service,
        work_records_service=work_records_service,
        notification_store=notifications,
        slack_notification_delivery=build_slack_delivery(environment, notifications),
        notification_channels=tuple(channel.key for channel in CHANNELS),
        notion_credentials_service=credentials,
        source_settings_service=source_settings,
        runtime_settings_service=RuntimeSettingsService(
            SQLiteRuntimeSettings(Database(environment.database)),
            RuntimeSettings(
                general=GeneralSettings(
                    "assignment"
                    if environment.relevance_policy == "assignment"
                    else "participation",
                    environment.snapshot_max_age_seconds,
                ),
                notion=NotionSettings(environment.notion_user_id),
            ),
        )
        if environment.database
        else None,
        preferences_store=SQLitePreferences(
            Database(environment.database),
            tuple(channel.key for channel in CHANNELS),
            notification_groups=groups,
        )
        if environment.database
        else None,
    )


def build_today_query(
    environment: AppEnvironment,
    settings: Settings | None,
    *,
    page_links: NotionPageLinksClient | None = None,
    reader: SnapshotReader | None = None,
    desktop: bool = False,
) -> TodayQuery | None:
    """캐시 조회에 필요한 사용자·파일·소스만 확인하며 Notion 토큰은 요구하지 않는다."""
    if environment.database is None or settings is None:
        return None
    user = environment.notion_user_id
    sources = tuple(
        source
        for source in settings.active_sources
        if (source.has_person_scope and user is not None)
        or (desktop and not source.has_person_scope)
    )
    if not sources and not desktop:
        return None
    if reader is None:
        reader = SQLiteWorkReader(Database(environment.database))
    context = TodayContext(
        user or "",
        sources,
        settings.completed_statuses if desktop else {},
        environment.snapshot_max_age_seconds,
        source_pages=settings.source_pages,
        desktop_areas={item.id: item.sort for item in settings.display if item.category is not None}
        if desktop
        else {},
        main_cards={
            item.id: MainListCard(
                category.key,
                config.main_list,
                {**FIELD_KINDS, **{column.id: column.kind for column in config.columns}},
            )
            for item in settings.display
            if (category := item.category) is not None
            and (config := category.configuration) is not None
            and config.main_list is not None
        }
        if desktop
        else {},
        display_columns={
            source.source_id: source.display_columns
            for source in sources
            if source.display_columns is not None
        }
        if desktop
        else {},
    )
    policy = select_policy(environment.relevance_policy)

    async def query(date: str | None) -> dict[str, object]:
        """같은 조회 유스케이스에 현재 시계와 저장 어댑터를 주입한다."""
        current = await with_page_links(context, page_links)
        return await get_my_today(date, reader, current, policy, now)

    return query


def build_task_queries(
    environment: AppEnvironment,
    settings: Settings | None,
    *,
    reader: SnapshotReader | None = None,
) -> tuple[TaskSearchQuery | None, TaskStatusesQuery | None]:
    """업무·QA 검색에도 현재 설정 소스와 본인 ID만 주입한다."""
    if environment.database is None or environment.notion_user_id is None or settings is None:
        return None, None
    sources = tuple(
        source
        for source in settings.active_sources
        if source.kind == "task" and source.has_person_scope
    )
    if not sources:
        return None, None
    if reader is None:
        reader = SQLiteWorkReader(Database(environment.database))
    context = TodayContext(
        environment.notion_user_id,
        sources,
        max_age_seconds=environment.snapshot_max_age_seconds,
        source_names={
            item.id: item.name or item.title
            for item in settings.display
            if item.category is not None
        },
    )
    policy = select_policy(environment.relevance_policy)

    async def query(search: TaskSearch) -> dict[str, object]:
        """인증 경계에서 검증한 검색 조건만 순수 조회에 전달한다."""
        return await search_my_tasks(search, reader, context, policy, now)

    async def statuses() -> dict[str, object]:
        """검색과 같은 스냅샷과 개인 권한으로 원본 상태 목록을 읽는다."""
        return await list_my_task_statuses(reader, context, policy, now)

    return query, statuses


async def with_page_links(
    context: TodayContext, service: NotionPageLinksClient | None
) -> TodayContext:
    """표시용 주소만 최신 설정으로 교체하고 수집 대상·정책은 유지한다."""
    if service is None:
        return context
    try:
        state = await asyncio.to_thread(service.read)
    except SnapshotError:
        raise QueryReadError() from None
    return replace(
        context,
        source_pages={item.source_id: item.url for item in state.sources if item.url is not None},
        page_links_revision=state.revision,
    )
