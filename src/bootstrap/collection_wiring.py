"""수동·정기 수집이 사용하는 원천과 스냅샷 저장소를 조립한다."""

import httpx

from src.application.ports import SnapshotStore, WorkSource
from src.application.source_settings import ProviderSources
from src.domain.meeting_reminders import TenMinuteMeetingReminderPolicy
from src.domain.retry import TransientRetryPolicy
from src.domain.task_changes import AssignedTaskChangePolicy
from src.infrastructure.notion.source import NotionSource
from src.infrastructure.sqlite.connection import Database
from src.infrastructure.sqlite.snapshots import SQLiteSnapshots


def build_collection(
    database: Database,
    client: httpx.AsyncClient,
    token: str,
    user_id: str | None,
    *,
    scheduled: bool,
) -> tuple[WorkSource, SnapshotStore]:
    """같은 알림 정책을 쓰고 예약 실행에만 주기 확인과 빠른 재시도를 적용한다."""
    reader = ProviderSources({"notion": NotionSource(token, client, user_id=user_id)})
    store = SQLiteSnapshots(
        database,
        scheduled=scheduled,
        notification_user_id=user_id,
        task_change_policy=AssignedTaskChangePolicy(),
        meeting_reminder_policy=TenMinuteMeetingReminderPolicy(),
        retry_policy=TransientRetryPolicy() if scheduled else None,
    )
    return reader, store
