"""정상 스냅샷과 회의 사전 알림을 원자적으로 연결한다."""

import json
import sqlite3
from collections.abc import Iterator, Sequence
from datetime import datetime
from itertools import batched

from src.application.errors import QueryReadError, SnapshotError
from src.application.models import SyncRun
from src.application.notion_links import notion_link
from src.domain.meeting_reminders import MeetingReminderPolicy
from src.domain.models import CalendarEvent, SourceMapping, WorkItem
from src.domain.work_dates import SEOUL, canonical_identity
from src.infrastructure.sqlite.decoding import decode_item
from src.infrastructure.sqlite.notification_deliveries import logical_completed
from src.infrastructure.sqlite.notification_keys import meeting_scope, notification_key


def meetings_with_jobs(
    connection: sqlite3.Connection, source_id: str, user: str, items: Sequence[WorkItem]
) -> Iterator[tuple[CalendarEvent, str, list[sqlite3.Row]]]:
    """이번 수집에 포함된 본인 예약을 묶어서 조회하고 원래 회의 순서로 넘긴다."""
    meetings = (item for item in items if isinstance(item, CalendarEvent))
    for batch in batched(meetings, 128):
        keys = [notification_key(f"meeting:{source_id}:{item.id}:{user}") for item in batch]
        placeholders = ",".join("?" for _ in keys)
        # 묶음마다 전체 대기 큐를 순회하지 않도록 현재 회의 키의 인덱스로 찾는다.
        rows = connection.execute(
            f"""SELECT id,logical_key,state,body,notion_url,meeting_start_at,meeting_mapping_hash
                FROM notification_jobs INDEXED BY notification_logical_idx
                WHERE logical_key IN ({placeholders})
                AND kind='meeting' AND state IN ('pending','delivering') ORDER BY logical_key,rowid""",
            keys,
        )
        grouped: dict[str, list[sqlite3.Row]] = {}
        for row in rows:
            grouped.setdefault(row["logical_key"], []).append(row)
        for item, logical in zip(batch, keys, strict=True):
            yield item, logical, grouped.get(logical, [])


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
    for item, logical, jobs in meetings_with_jobs(connection, run.source_id, user, items):
        current.add(item.id)
        timing = policy.schedule(item, user, now)
        start = timing.start.timestamp() if timing is not None else None
        existing = None
        for job in jobs:
            if (
                start is None
                or job["meeting_start_at"] != start
                or job["meeting_mapping_hash"] != run.mapping_hash
            ):
                connection.execute(
                    "UPDATE notification_jobs SET state='obsolete' WHERE id=?", (job["id"],)
                )
            elif existing is None:
                existing = job
        if timing is None or (existing is not None and existing["state"] == "delivering"):
            continue
        body = f"{timing.start.astimezone(SEOUL):%H:%M} · {item.title[:160]}"
        url = notion_link(item.url)
        if existing is not None and body == existing["body"] and url == existing["notion_url"]:
            continue
        # 새 예약이나 대기 중 본문을 확정하기 전에는 채널별 전달 완료 여부를 다시 검사한다.
        if logical_completed(connection, logical):
            continue
        if existing is not None:
            connection.execute(
                "UPDATE notification_jobs SET body=?,notion_url=? WHERE id=?",
                (body, url, existing["id"]),
            )
            continue
        identifier = notification_key(f"{logical}:{run.id}")
        connection.execute(
            """INSERT INTO notification_jobs(id,logical_key,kind,title,body,notion_url,due_at,expires_at,state,
                source_key,meeting_source_id,meeting_item_id,meeting_mapping_hash,meeting_start_at)
            VALUES(?,?,'meeting','회의 시작 알림',?,?,?,?,'pending',?,?,?,?,?)""",
            (
                identifier,
                logical,
                body,
                url,
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
    )
    missing = [(row["id"],) for row in rows if row["meeting_item_id"] not in current]
    connection.executemany("UPDATE notification_jobs SET state='obsolete' WHERE id=?", missing)


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
