"""정상 스냅샷과 회의 사전 알림을 원자적으로 연결한다."""

import json
import sqlite3
from collections.abc import Sequence
from datetime import datetime

from src.application.errors import QueryReadError, SnapshotError
from src.application.models import SyncRun
from src.application.notion_links import notion_link
from src.domain.meeting_reminders import MeetingReminderPolicy
from src.domain.models import CalendarEvent, SourceMapping, WorkItem
from src.domain.work_dates import SEOUL, canonical_identity
from src.infrastructure.sqlite.decoding import decode_item
from src.infrastructure.sqlite.notification_deliveries import logical_completed
from src.infrastructure.sqlite.notification_keys import meeting_scope, notification_key


def enqueue_meeting_reminders(
    connection: sqlite3.Connection,
    run: SyncRun,
    items: Sequence[WorkItem],
    user_id: str | None,
    completed_at: str,
    policy: MeetingReminderPolicy | None,
) -> None:
    """정상 수집에서만 예약을 정정하고 미확인 누락은 발송하지 않는다."""
    if run.kind != "meeting" or user_id is None or policy is None:
        return
    user = canonical_identity(user_id)
    scope = meeting_scope(run.source_id, user, run.mapping_hash)
    now = datetime.fromisoformat(completed_at)
    current: set[str] = set()
    for item in items:
        if not isinstance(item, CalendarEvent):
            continue
        current.add(item.id)
        logical = notification_key(f"meeting:{run.source_id}:{item.id}:{user}")
        timing = policy.schedule(item, user, now)
        start = timing.start.timestamp() if timing is not None else None
        connection.execute(
            """UPDATE notification_jobs SET state='obsolete'
            WHERE kind='meeting' AND logical_key=? AND state IN ('pending','delivering')
            AND (? IS NULL OR meeting_start_at<>? OR meeting_mapping_hash<>?)""",
            (logical, start, start, run.mapping_hash),
        )
        if timing is None:
            continue
        body = f"{timing.start.astimezone(SEOUL):%H:%M} · {item.title[:160]}"
        if logical_completed(connection, logical):
            continue
        existing = connection.execute(
            """SELECT id,state FROM notification_jobs WHERE logical_key=?
            AND state IN ('pending','delivering') LIMIT 1""",
            (logical,),
        ).fetchone()
        if existing is not None:
            if existing["state"] == "pending":
                connection.execute(
                    "UPDATE notification_jobs SET body=?,notion_url=? WHERE id=?",
                    (body, notion_link(item.url), existing["id"]),
                )
            continue  # 이미 요청했거나 결과가 불명확한 회의는 시간 변경으로 재발송하지 않는다.
        identifier = notification_key(f"{logical}:{run.id}")
        connection.execute(
            """INSERT INTO notification_jobs(id,logical_key,kind,title,body,notion_url,due_at,expires_at,state,
                source_key,meeting_source_id,meeting_item_id,meeting_mapping_hash,meeting_start_at)
            VALUES(?,?,'meeting','회의 시작 알림',?,?,?,?,'pending',?,?,?,?,?)""",
            (
                identifier,
                logical,
                body,
                notion_link(item.url),
                timing.due.timestamp(),
                start,
                scope,
                run.source_id,
                item.id,
                run.mapping_hash,
                start,
            ),
        )
    # 누락은 삭제 확정이 아니다. 마지막 스냅샷은 보존하되 확인되지 않은 예약만 멈춘다.
    rows = connection.execute(
        """SELECT id,meeting_item_id FROM notification_jobs WHERE kind='meeting'
        AND source_key=? AND state IN ('pending','delivering')""",
        (scope,),
    ).fetchall()
    for row in rows:
        if row["meeting_item_id"] not in current:
            connection.execute(
                "UPDATE notification_jobs SET state='obsolete' WHERE id=?", (row["id"],)
            )


def meeting_job_current(row: dict[str, object], user_id: str | None, now: datetime) -> bool:
    """실행권 뒤에도 현재 본인 참석·유형·취소·시각·매핑을 다시 검사한다."""
    if user_id is None or row["current_availability"] != "present":
        return False
    if row["current_mapping"] != row["meeting_mapping_hash"]:
        return False
    payload, source_id = row["current_payload"], row["meeting_source_id"]
    if not isinstance(payload, str) or not isinstance(source_id, str):
        raise SnapshotError("invalid_snapshot")
    try:
        value: object = json.loads(payload)
        item = decode_item(value, SourceMapping("meeting", source_id, {}))
        if not isinstance(item, CalendarEvent):
            raise QueryReadError()
        start = row["meeting_start_at"]
        # 예약 정책을 바꾸더라도 승인에는 저장한 시작 시각과 현재 회의 사실을 사용한다.
        return (
            item.id == row["meeting_item_id"]
            and item.event_type == "meeting"
            and not item.cancelled
            and canonical_identity(user_id) in {canonical_identity(p) for p in item.participant_ids}
            and item.period is not None
            and item.period.kind == "instant"
            and datetime.fromisoformat(item.period.start).timestamp() == start
            and isinstance(start, (int, float))
            and now.timestamp() < start
        )
    except (ValueError, TypeError, QueryReadError):
        raise SnapshotError("invalid_snapshot") from None
