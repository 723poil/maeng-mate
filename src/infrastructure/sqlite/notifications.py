"""수집·근무 기록과 원자적으로 연결되는 영속 알림 큐다."""

import asyncio
import json
import sqlite3
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, date, datetime, timedelta

from src.application.notifications import DeliveryResult, NotificationJob, parse_delivery_result
from src.application.notion_links import notion_link
from src.domain.daily_work import DayRecord, DeparturePolicy
from src.domain.models import CalendarEvent, WorkItem
from src.domain.notification_timing import DEPARTURE_REMINDER_MINUTES, calendar_alert_expiry
from src.domain.work_dates import SEOUL, canonical_identity
from src.infrastructure.sqlite.connection import Database
from src.infrastructure.sqlite.field_alerts import field_alert_current
from src.infrastructure.sqlite.meeting_reminders import meeting_job_current
from src.infrastructure.sqlite.notification_deliveries import (
    claim_delivery,
    finish_deliveries,
    logical_completed,
    prepare_deliveries,
)
from src.infrastructure.sqlite.notification_keys import departure_key, notification_key
from src.infrastructure.sqlite.preferences import read_preferences
from src.infrastructure.sqlite.task_changes import task_job_current


def enqueue_departure(
    connection: sqlite3.Connection, record: DayRecord, policy: DeparturePolicy, now: datetime
) -> None:
    """근무 정정 트랜잭션 안에서 이전 대기 예약을 무효화한다."""
    day = record.day.isoformat()
    connection.execute(
        "UPDATE notification_jobs SET state='obsolete' WHERE day=? AND kind='departure' AND revision<>? AND state IN ('pending','delivering')",
        (day, record.revision),
    )
    result = policy.calculate(record)
    if result.departure is None or record.checkout_at is not None:
        connection.execute(
            "UPDATE notification_jobs SET state='obsolete' WHERE day=? AND kind='departure' AND state IN ('pending','delivering')",
            (day,),
        )
        return
    for minutes in DEPARTURE_REMINDER_MINUTES:
        due = result.departure - timedelta(minutes=minutes)
        if due <= now:
            continue  # 이미 지난 퇴근 알림을 새 입력만으로 뒤늦게 만들지 않는다.
        logical = departure_key(day, minutes)
        if logical_completed(connection, logical):
            continue
        identifier = notification_key(f"{logical}:{record.revision}")
        # 계산 전략이 바뀌어도 같은 근무 버전의 미발송 예약은 새 시각으로 맞춘다.
        connection.execute(
            """INSERT INTO notification_jobs(id,logical_key,kind,title,body,due_at,expires_at,state,day,revision)
            VALUES(?,?,'departure',?,?,?,?,'pending',?,?) ON CONFLICT(id) DO UPDATE SET
              title=excluded.title,body=excluded.body,due_at=excluded.due_at,expires_at=excluded.expires_at
            WHERE notification_jobs.kind='departure' AND notification_jobs.state='pending'""",
            (
                identifier,
                logical,
                f"퇴근 {minutes}분 전",
                f"퇴근 예정 {result.departure.astimezone(SEOUL):%H:%M}",
                due.timestamp(),
                due.timestamp() + 60,
                day,
                record.revision,
            ),
        )


def enqueue_calendar(
    connection: sqlite3.Connection,
    source_id: str,
    items: Sequence[WorkItem],
    user_id: str | None,
    completed_at: str,
    previous_mapping: str | None,
    mapping_hash: str,
) -> None:
    """정상 스냅샷 확정 전에 기존 본인 일정과 비교해 새 항목만 예약한다."""
    if user_id is None:
        return
    user = canonical_identity(user_id)
    source_key = notification_key(f"{source_id}:{user}")
    if previous_mapping != mapping_hash:
        connection.execute(
            "UPDATE notification_jobs SET state='obsolete' WHERE source_key=? AND state IN ('pending','delivering')",
            (source_key,),
        )
        return  # 최초 수집·매핑 변경은 기준값으로만 사용한다.
    previous: set[str] = set()
    rows = connection.execute(
        "SELECT item_id,payload FROM item_snapshots WHERE source_id=?", (source_id,)
    ).fetchall()
    for row in rows:
        raw: object = json.loads(row["payload"])
        if isinstance(raw, dict):
            people = raw.get("participant_ids")
            if isinstance(people, list) and any(
                isinstance(person, str) and canonical_identity(person) == user for person in people
            ):
                previous.add(row["item_id"])
    now = datetime.fromisoformat(completed_at)
    for item in items:
        if not isinstance(item, CalendarEvent):
            continue
        logical = notification_key(f"calendar:{source_id}:{item.id}:{user}")
        relevant = any(canonical_identity(person) == user for person in item.participant_ids)
        expires = calendar_alert_expiry(item.period, now)
        if item.cancelled or not relevant or expires is None:
            connection.execute(
                "UPDATE notification_jobs SET state='obsolete' WHERE logical_key=? AND state IN ('pending','delivering')",
                (logical,),
            )
            continue
        if item.id in previous:
            connection.execute(
                "UPDATE notification_jobs SET body=?,notion_url=?,expires_at=MIN(expires_at,?) WHERE logical_key=? AND state='pending'",
                (item.title[:180], notion_link(item.url), expires, logical),
            )
            continue
        connection.execute(
            """INSERT INTO notification_jobs(id,logical_key,kind,title,body,notion_url,due_at,expires_at,state,source_key)
            VALUES(?,?,'calendar','새 일정',?,?,?,?,'pending',?) ON CONFLICT(id) DO NOTHING""",
            (
                logical,
                logical,
                item.title[:180],
                notion_link(item.url),
                now.timestamp(),
                expires,
                source_key,
            ),
        )


def utc_now() -> datetime:
    """큐 비교 시점은 UTC를 사용한다."""
    return datetime.now(UTC)


class SQLiteNotifications:
    """실행권·중복 방지·불명확한 전달 결과를 DB에 보존한다."""

    def __init__(
        self,
        database: Database,
        *,
        now: Callable[[], datetime] = utc_now,
        source_keys: tuple[str, ...] | None = None,
        user_id: str | None = None,
        channels: tuple[str, ...] = ("macos",),
        qa_scopes: tuple[str, ...] = (),
        source_channels: Mapping[str, tuple[str, ...]] | None = None,
        source_types: Mapping[str, tuple[str, ...]] | None = None,
        source_titles: Mapping[str, Mapping[str, str]] | None = None,
        notification_groups: Mapping[str, str] | None = None,
    ) -> None:
        """큐 조회·발송 승인에 같은 시계를 사용한다."""
        self._database = database
        self._now = now
        self._source_keys = source_keys
        self._user_id = user_id
        self._channels = channels
        self._qa_scopes = qa_scopes
        self._source_channels = dict(source_channels or {})
        self._source_types = dict(source_types or {})
        self._source_titles = dict(source_titles or {})
        self._notification_groups = dict(notification_groups or {})

    def _channels_for(self, key: object) -> tuple[str, ...]:
        """설정된 소스는 전역 채널과 공통인 채널만 사용할 수 있다."""
        if not isinstance(key, str) or key not in self._source_channels:
            return self._channels
        return tuple(channel for channel in self._channels if channel in self._source_channels[key])

    async def claim(self) -> tuple[NotificationJob, ...]:
        """짧은 쓰기 트랜잭션에서 알림을 최대 한 개 가져온다."""
        return await asyncio.to_thread(self._claim)

    def _claim(self) -> tuple[NotificationJob, ...]:
        """멈춘 요청은 unknown으로 두고 같은 논리 알림의 재전송을 막는다."""
        now = self._now().timestamp()
        with self._database.transaction() as connection:
            finish_deliveries(connection, now)
            preferences = read_preferences(connection, self._channels)
            connection.execute(
                "UPDATE notification_jobs SET state='unknown' WHERE state IN ('delivering','obsolete') AND token IS NOT NULL AND claimed_at<?",
                (now - 30,),
            )
            connection.execute(
                "UPDATE notification_jobs SET state='expired' WHERE state='pending' AND expires_at<?",
                (now,),
            )
            scope = ""
            scope_values: tuple[object, ...] = ()
            if self._source_keys is not None:
                if self._source_keys:
                    placeholders = ",".join("?" for _ in self._source_keys)
                    scope = f" AND (job.kind='departure' OR job.source_key IN ({placeholders}))"
                    scope_values = tuple(self._source_keys)
                else:
                    scope = " AND job.kind='departure'"
            rows = connection.execute(
                """SELECT job.* FROM notification_jobs AS job
                WHERE job.state='pending' AND job.due_at<=? AND job.expires_at>=?
                """
                + scope
                + " ORDER BY CASE WHEN job.kind='departure' THEN 0 WHEN job.kind='meeting' THEN 1 WHEN job.kind='sync_error' THEN 2 ELSE 3 END,due_at,id LIMIT 1",
                (now, now, *scope_values),
            ).fetchall()
            for row in rows:
                prepare_deliveries(
                    connection,
                    row,
                    preferences,
                    self._channels_for(row["source_key"]),
                    self._qa_scopes,
                    self._source_types.get(row["source_key"]),
                    self._notification_groups.get(row["source_key"]),
                    self._source_titles.get(row["source_key"]),
                )
            return claim_delivery(connection, now, self._source_keys, self._channels)

    async def authorize(self, identifier: str, token: str) -> bool:
        """실행권·현재 사실·최신 설정을 발송 직전에 같은 읽기로 확인한다."""
        return await asyncio.to_thread(self._authorize, identifier, token)

    def _authorize(self, identifier: str, token: str) -> bool:
        """한 DB 스냅샷에서 근무 버전과 구독 선택을 함께 확인한다."""
        now = self._now()
        with self._database.transaction(read_only=True) as connection:
            rows: list[dict[str, object]] = [
                dict(row)
                for row in connection.execute(
                    """SELECT job.day,job.revision,job.kind,job.source_key,record.revision AS current_revision,
                job.meeting_source_id,job.meeting_item_id,job.meeting_mapping_hash,job.meeting_start_at,
                failure.status AS sync_failure_status,failure.mapping_hash AS sync_failure_mapping,
                job.sync_mapping_hash,delivery.channel,delivery.kinds,
                EXISTS(SELECT 1 FROM sync_runs AS recovery WHERE recovery.source_id=job.sync_source_id
                    AND recovery.status='succeeded' AND recovery.id>job.sync_failure_run_id) AS sync_recovered,
                event.source_id AS change_source,event.user_key AS change_user,event.mapping_hash AS change_mapping,
                event.after_hash AS change_after_hash,snapshot.payload AS current_payload,
                field.field_id,field.value_hash AS field_hash,field.mapping_hash AS field_mapping,field.person_scoped AS field_person_scoped,
                snapshot.mapping_hash AS current_mapping,snapshot.availability AS current_availability
            FROM notification_deliveries AS delivery JOIN notification_jobs AS job ON job.id=delivery.job_id LEFT JOIN day_records AS record ON record.day=job.day
            LEFT JOIN sync_runs AS failure ON failure.id=job.sync_failure_run_id
            LEFT JOIN task_changes AS event ON event.id=job.task_change_id
            LEFT JOIN source_alert_events AS field ON field.job_id=job.id
            LEFT JOIN item_snapshots AS snapshot
                ON snapshot.source_id=COALESCE(event.source_id,job.meeting_source_id,field.source_id)
                AND snapshot.item_id=COALESCE(event.item_id,job.meeting_item_id,field.item_id)
            WHERE delivery.id=? AND delivery.token=? AND delivery.state='delivering' AND delivery.revoked=0 AND job.state='delivering' AND job.expires_at>=? AND job.due_at<=?""",
                    (identifier, token, now.timestamp(), now.timestamp()),
                ).fetchall()
            ]
            preferences = read_preferences(connection, self._channels)
        if not rows:
            return False
        raw_kinds = rows[0]["kinds"]
        if not isinstance(raw_kinds, str):
            return False
        kinds: object = json.loads(raw_kinds)
        if not isinstance(kinds, list):
            return False
        selected = tuple(kind for kind in kinds if isinstance(kind, str))
        source_key = rows[0]["source_key"]
        allowed_types = self._source_types.get(source_key) if isinstance(source_key, str) else None
        if allowed_types is not None:
            selected = tuple(kind for kind in selected if kind in allowed_types)
        channel = rows[0]["channel"]
        if (
            not isinstance(channel, str)
            or not preferences.permits(
                channel,
                selected,
                self._notification_groups.get(source_key) if isinstance(source_key, str) else None,
            )
            or channel not in self._channels_for(rows[0]["source_key"])
        ):
            return False
        if (
            rows[0]["kind"] != "departure"
            and self._source_keys is not None
            and rows[0]["source_key"] not in self._source_keys
        ):
            return False
        if rows[0]["kind"] == "sync_error":
            return (
                rows[0]["sync_failure_status"] == "failed"
                and rows[0]["sync_failure_mapping"] == rows[0]["sync_mapping_hash"]
                and rows[0]["sync_recovered"] == 0
            )
        if rows[0]["kind"] == "meeting":
            return meeting_job_current(rows[0], self._user_id, now)
        if rows[0]["kind"] == "task":
            return task_job_current(rows[0], self._user_id)
        if rows[0]["kind"] == "source_change":
            return field_alert_current(rows[0], self._user_id)
        if rows[0]["kind"] == "departure":
            day = rows[0]["day"]
            if not isinstance(day, str):
                return False
            recorded_day = date.fromisoformat(day)
            # 예약 생성은 기록의 휴무일 규칙을 검사한다. 전달은 같은 기록 버전만 허용한다.
            return (
                recorded_day == now.astimezone(SEOUL).date()
                and rows[0]["revision"] == rows[0]["current_revision"]
            )
        return rows[0]["day"] is None or rows[0]["revision"] == rows[0]["current_revision"]

    async def reserve_slack(self, identifier: str, token: str) -> NotificationJob | None:
        """현재 사실을 확인한 실행권만 발송 시작 표식을 얻는다."""
        if not await self.authorize(identifier, token):
            return None
        return await asyncio.to_thread(self._reserve_slack, identifier, token)

    def _reserve_slack(self, identifier: str, token: str) -> NotificationJob | None:
        """동일 실행권의 동시 HTTP 요청·재호출을 원자적으로 차단한다."""
        with self._database.transaction() as connection:
            row = connection.execute(
                """SELECT d.id,d.channel,j.title,j.body,j.notion_url FROM notification_deliveries d
                JOIN notification_jobs j ON j.id=d.job_id
                WHERE d.id=? AND d.token=? AND d.channel='slack' AND d.state='delivering'
                AND d.revoked=0 AND d.request_started_at IS NULL AND j.state='delivering'
                AND j.due_at<=? AND j.expires_at>=?""",
                (identifier, token, self._now().timestamp(), self._now().timestamp()),
            ).fetchone()
            if row is None:
                return None
            connection.execute(
                "UPDATE notification_deliveries SET request_started_at=? WHERE id=?",
                (self._now().timestamp(), identifier),
            )
            return NotificationJob(
                identifier,
                token,
                row["title"],
                row["body"],
                row["channel"],
                notion_link(row["notion_url"]),
            )

    async def complete_slack(self, identifier: str, token: str, result: str) -> None:
        """영속 표식을 얻은 서버만 실제 결과를 확정한다."""
        await asyncio.to_thread(
            self._ack, identifier, token, parse_delivery_result(result), server=True
        )

    async def ack(self, identifier: str, token: str, result: str) -> None:
        """이전 실행의 결과가 다른 실행권을 덮어쓰지 못하게 한다."""
        await asyncio.to_thread(self._ack, identifier, token, parse_delivery_result(result))

    def _ack(
        self, identifier: str, token: str, result: DeliveryResult, *, server: bool = False
    ) -> None:
        """정정 중인 이전 요청의 실제 결과도 누락하지 않는다."""
        with self._database.transaction() as connection:
            connection.execute(
                """UPDATE notification_deliveries SET state=? WHERE id=? AND token=? AND state='delivering'
                AND (channel<>'slack' OR request_started_at IS NULL OR ?)""",
                (result, identifier, token, server),
            )
            finish_deliveries(connection, self._now().timestamp())
