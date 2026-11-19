"""현재 개인 소스와 읽기 어댑터를 달력 조회에 조립한다."""

from src.application.calendar import CalendarService, CalendarSource
from src.application.work_settings import Settings
from src.bootstrap.clock import now
from src.bootstrap.environment import AppEnvironment
from src.bootstrap.policies import select_policy
from src.infrastructure.sqlite.connection import Database
from src.infrastructure.sqlite.day_records import SQLiteDayStore
from src.infrastructure.sqlite.work_reader import SQLiteWorkReader


def build_calendar_service(
    environment: AppEnvironment, settings: Settings | None
) -> CalendarService | None:
    """Notion 계정이 없더라도 개인 출퇴근 기록을 달력에서 볼 수 있게 한다."""
    if environment.database is None:
        return None
    database = Database(environment.database)
    sources: list[CalendarSource] = []
    if settings is not None:
        displays = {item.id: item for item in settings.display}
        for mapping in settings.active_sources:
            if not environment.notion_user_id and mapping.has_person_scope:
                continue
            display = displays[mapping.source_id]
            sources.append(
                CalendarSource(mapping, display.calendar_visible, display.color, display.key)
            )
    return CalendarService(
        SQLiteWorkReader(database),
        SQLiteDayStore(database),
        tuple(sources),
        select_policy(environment.relevance_policy),
        user_id=environment.notion_user_id or "",
        max_age_seconds=environment.snapshot_max_age_seconds,
        now=now,
    )
