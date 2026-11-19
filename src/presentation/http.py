"""HTTP 인증과 응답 변환을 담당한다."""

import asyncio
import hashlib
import hmac
import time
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager, nullcontext

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from mcp.server.fastmcp import FastMCP
from starlette.middleware.base import RequestResponseEndpoint
from starlette.responses import Response

from src.application.calendar import CalendarService
from src.application.date_inputs import parse_day
from src.application.day_ports import DayUseCases
from src.application.errors import InvalidDateError, QueryReadError, SnapshotError
from src.application.mcp_access import McpAccessStore, McpSettingsClient, is_mcp_read_route
from src.application.notifications import NotificationStore
from src.application.notion_credentials import NotionCredentialsClient
from src.application.notion_page_links import NotionPageLinksClient
from src.application.periodic import IntervalUpdate
from src.application.ports import PeriodicStatusQuery, TodayQuery
from src.application.preferences import PreferencesStore
from src.application.runtime_settings_service import RuntimeSettingsClient
from src.application.slack_notifications import SlackNotificationDelivery
from src.application.slack_settings import SlackSettingsClient
from src.application.source_settings import SourceSettingsClient
from src.application.task_search import TaskSearchQuery
from src.application.work_records import WorkRecordsService
from src.application.work_rules_settings import WorkRulesClient
from src.presentation.calendar_http import create_calendar_router
from src.presentation.collection_http import create_collection_router
from src.presentation.day_http import create_day_router
from src.presentation.mcp_settings_http import create_mcp_settings_router
from src.presentation.notification_http import create_notification_router
from src.presentation.notion_credentials_http import create_notion_credentials_router
from src.presentation.notion_page_links_http import create_page_links_router
from src.presentation.preferences_http import create_preferences_router
from src.presentation.runtime_settings_http import create_runtime_settings_router
from src.presentation.slack_settings_http import create_slack_settings_router
from src.presentation.source_settings_http import create_source_settings_router
from src.presentation.task_http import create_task_router
from src.presentation.work_records_http import create_work_records_router
from src.presentation.work_rules_http import create_work_rules_router


def create_app(
    token: str,
    query_status: Callable[[], dict[str, object]],
    query_today: TodayQuery | None = None,
    *,
    query_desktop_today: TodayQuery | None = None,
    query_periodic: PeriodicStatusQuery | None = None,
    update_interval: IntervalUpdate | None = None,
    day_service: DayUseCases | None = None,
    notification_store: NotificationStore | None = None,
    slack_notification_delivery: SlackNotificationDelivery | None = None,
    query_tasks: TaskSearchQuery | None = None,
    preferences_store: PreferencesStore | None = None,
    notification_channels: tuple[str, ...] = ("macos",),
    runtime_settings_service: RuntimeSettingsClient | None = None,
    notion_credentials_service: NotionCredentialsClient | None = None,
    page_links_service: NotionPageLinksClient | None = None,
    mcp_service: McpSettingsClient | None = None,
    mcp_access: McpAccessStore | None = None,
    mcp_server: FastMCP | None = None,
    mcp_query_client: AbstractAsyncContextManager[object] | None = None,
    on_mcp_ready: Callable[[bool], None] | None = None,
    work_records_service: WorkRecordsService | None = None,
    work_rules_service: WorkRulesClient | None = None,
    source_settings_service: SourceSettingsClient | None = None,
    calendar_service: CalendarService | None = None,
    slack_settings_service: SlackSettingsClient | None = None,
) -> FastAPI:
    """설정과 어댑터를 만들지 않고 주입받은 상태 조회만 노출한다."""

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        """MCP 수명을 같은 앱의 시작·종료에 묶는다."""
        if mcp_server is None:
            yield
            return
        # 도구 세션을 먼저 종료한 뒤 그 세션들이 사용한 루프백 연결을 닫는다.
        async with mcp_query_client or nullcontext(), mcp_server.session_manager.run():
            if on_mcp_ready is not None:
                on_mcp_ready(True)
            try:
                yield
            finally:
                if on_mcp_ready is not None:
                    on_mcp_ready(False)

    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
    expected = hashlib.sha256(token.encode()).digest()

    @app.middleware("http")
    async def authenticate(request: Request, call_next: RequestResponseEndpoint) -> Response:
        """모든 경로의 인증과 개인 응답의 캐시 금지를 적용한다."""
        header = request.headers.get("Authorization", "")
        raw = header.removeprefix("Bearer ")
        candidate = hashlib.sha256(raw.encode()).digest()
        internal = header.startswith("Bearer ") and hmac.compare_digest(expected, candidate)
        mcp_path = request.url.path == "/mcp" or request.url.path.startswith("/mcp/")
        response: Response
        if not header.startswith("Bearer ") or len(header) > 4096:
            response = JSONResponse({"detail": "Unauthorized"}, status_code=401)
        elif internal and not mcp_path:
            response = await call_next(request)
        elif internal:
            response = JSONResponse({"detail": "MCP access token required"}, status_code=403)
        else:
            try:
                allowed = mcp_access is not None and await asyncio.to_thread(
                    mcp_access.authorize, candidate.hex(), time.time()
                )
            except SnapshotError:
                response = JSONResponse({"detail": "Authentication unavailable"}, status_code=503)
            else:
                if not allowed:
                    response = JSONResponse({"detail": "Unauthorized"}, status_code=401)
                elif mcp_path or is_mcp_read_route(request.method, request.url.path):
                    request.scope["maeng.mcp_read_only"] = True
                    response = await call_next(request)
                else:
                    response = JSONResponse({"detail": "Read-only token"}, status_code=403)
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.get("/v1/status")
    async def status() -> dict[str, object]:
        """HTTP 요청을 상태 조회 유스케이스에 연결한다."""
        result = query_status()
        if query_periodic is not None:
            try:
                periodic = await query_periodic()
            except (SnapshotError, ValueError):
                raise HTTPException(503, detail="정기 수집 상태를 조회할 수 없습니다.") from None
            result["periodicSync"] = periodic
            result["automaticExecution"] = (
                "enabled" if periodic.get("enabled") is True else "disabled"
            )
            result["phase"] = "periodic_sync"
        return result

    @app.get("/v1/today")
    async def today(
        request: Request, date: str | None = None, desktop: bool = False
    ) -> dict[str, object]:
        """날짜만 조회에 전달하고 설정·저장 오류를 안전하게 알린다."""
        try:
            if date is not None:
                parse_day(date)
            query = query_today
            if desktop and not request.scope.get("maeng.mcp_read_only"):
                query = query_desktop_today or query
            if query is None:
                raise HTTPException(503, detail="개인 업무 조회 설정이 필요합니다.")
            return await query(date)
        except InvalidDateError:
            raise HTTPException(400, detail="날짜는 유효한 YYYY-MM-DD 형식이어야 합니다.") from None
        except QueryReadError:
            raise HTTPException(503, detail="저장된 업무 자료를 조회할 수 없습니다.") from None

    app.include_router(create_collection_router(update_interval))
    app.include_router(create_task_router(query_tasks))
    app.include_router(create_day_router(day_service))
    app.include_router(create_work_records_router(work_records_service))
    app.include_router(create_work_rules_router(work_rules_service))
    app.include_router(create_source_settings_router(source_settings_service))
    app.include_router(create_calendar_router(calendar_service))
    app.include_router(create_slack_settings_router(slack_settings_service))
    app.include_router(create_notification_router(notification_store, slack_notification_delivery))
    app.include_router(create_preferences_router(preferences_store, notification_channels))
    app.include_router(create_runtime_settings_router(runtime_settings_service))
    app.include_router(create_notion_credentials_router(notion_credentials_service))
    app.include_router(create_mcp_settings_router(mcp_service))
    if mcp_server is not None:
        app.mount("/mcp", mcp_server.streamable_http_app())
    app.include_router(create_page_links_router(page_links_service))
    return app
