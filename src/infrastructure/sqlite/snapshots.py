"""SQLite 트랜잭션으로 실행 lease와 정상 스냅샷을 보존한다."""

import asyncio
import sqlite3
import time
from collections.abc import Callable, Sequence
from datetime import UTC, datetime

from src.application.errors import SOURCE_ERROR_CODES, SnapshotError
from src.application.models import SyncRun
from src.domain.meeting_reminders import MeetingReminderPolicy
from src.domain.models import SourceMapping, WorkItem
from src.domain.retry import RetryPolicy
from src.domain.task_changes import TaskChangePolicy
from src.infrastructure.sqlite.connection import Database
from src.infrastructure.sqlite.field_alerts import record_field_alerts, seed_converted_baselines
from src.infrastructure.sqlite.meeting_reminders import enqueue_meeting_reminders
from src.infrastructure.sqlite.notifications import enqueue_calendar
from src.infrastructure.sqlite.schedules import advance_schedule, fail_schedule
from src.infrastructure.sqlite.serialization import source_fingerprint, staging_rows
from src.infrastructure.sqlite.sync_alerts import (
    enqueue_sync_failure,
    resolve_sync_failure,
    saved_failure_rules,
)
from src.infrastructure.sqlite.task_changes import record_task_changes


class SQLiteSnapshots:
    """동시 쓰기와 이전 세대의 뒤늦은 확정을 차단한다."""

    def __init__(
        self,
        database: Database,
        *,
        scheduled: bool = False,
        clock: Callable[[], float] = time.time,
        notification_user_id: str | None = None,
        task_change_policy: TaskChangePolicy | None = None,
        meeting_reminder_policy: MeetingReminderPolicy | None = None,
        retry_policy: RetryPolicy | None = None,
    ) -> None:
        """교체 가능한 저장 계약에 필요한 DB 경계만 받는다."""
        self._database = database
        self._scheduled = scheduled
        self._clock = clock
        self._notification_user_id = notification_user_id
        self._task_change_policy = task_change_policy
        self._meeting_reminder_policy = meeting_reminder_policy
        self._retry_policy = retry_policy

    async def begin_run(self, source: SourceMapping, started_at: str) -> SyncRun:
        """수집 시작의 짧은 DB 작업만 스레드에서 실행한다."""
        return await asyncio.to_thread(self._begin_run, source, started_at)

    def _begin_run(self, source: SourceMapping, started_at: str) -> SyncRun:
        """BEGIN IMMEDIATE 안에서 유효 lease를 확인하고 새 세대를 발급한다."""
        with self._database.transaction() as connection:
            connection.execute(
                "INSERT INTO work_source_state(source_id,kind) VALUES(?,?) ON CONFLICT DO NOTHING",
                (source.source_id, source.kind),
            )
            row = connection.execute(
                "SELECT generation,lease_until FROM work_source_state WHERE source_id=?",
                (source.source_id,),
            ).fetchone()
            epoch = self._clock()
            if row is None:
                raise SnapshotError("storage_failed")
            if row["lease_until"] is not None and row["lease_until"] > epoch:
                raise SnapshotError("source_busy")
            if self._scheduled:
                schedule = connection.execute(
                    "SELECT next_run_at FROM sync_schedules WHERE source_id=?", (source.source_id,)
                ).fetchone()
                if schedule is None:
                    raise SnapshotError("storage_failed")
                if schedule["next_run_at"] > epoch:
                    raise SnapshotError("source_not_due")
            connection.execute(
                """UPDATE sync_schedules SET retry_count=CASE WHEN ? THEN retry_count ELSE 0 END,
                retry_pending=0 WHERE source_id=?""",
                (self._scheduled, source.source_id),
            )
            generation = int(row["generation"]) + 1
            connection.execute(
                "UPDATE work_source_state SET generation=?,kind=?,lease_until=? WHERE source_id=?",
                (generation, source.kind, epoch + 300, source.source_id),
            )
            connection.execute(
                "UPDATE sync_runs SET status='abandoned' WHERE source_id=? AND status='running'",
                (source.source_id,),
            )
            mapping_hash = source_fingerprint(source)
            cursor = connection.execute(
                "INSERT INTO sync_runs(source_id,generation,mapping_hash,started_at,status) VALUES(?,?,?,?,'running')",
                (source.source_id, generation, mapping_hash, started_at),
            )
            if cursor.lastrowid is None:
                raise SnapshotError("storage_failed")
            return SyncRun(
                cursor.lastrowid, source.source_id, source.kind, generation, mapping_hash, source
            )

    def _previous_mapping(self, connection: sqlite3.Connection, run: SyncRun) -> str | None:
        """현재 실행권을 확인하고 같은 행에서 직전 정상 수집의 매핑을 읽는다."""
        row = connection.execute(
            """SELECT state.lease_until, state.mapping_hash
            FROM work_source_state AS state JOIN sync_runs AS run
              ON run.source_id=state.source_id AND run.generation=state.generation
            WHERE state.source_id=? AND state.generation=? AND run.id=? AND run.status='running'""",
            (run.source_id, run.generation, run.id),
        ).fetchone()
        if row is None or row["lease_until"] is None or row["lease_until"] <= self._clock():
            raise SnapshotError("stale_run")
        previous: object = row["mapping_hash"]
        if previous is not None and not isinstance(previous, str):
            raise SnapshotError("invalid_snapshot")
        return previous

    async def complete_run(
        self, run: SyncRun, items: Sequence[WorkItem], completed_at: str
    ) -> None:
        """완전 수집 결과만 작업 스레드에서 원자적으로 확정한다."""
        await asyncio.to_thread(self._complete_run, run, items, completed_at)

    def _complete_run(self, run: SyncRun, items: Sequence[WorkItem], completed_at: str) -> None:
        """부분 저장 오류가 나면 staging과 스냅샷을 함께 롤백한다."""
        with self._database.transaction() as connection:
            previous_mapping = self._previous_mapping(connection, run)
            if run.kind == "meeting" and (
                run.source is not None and run.source.allows_event_alert("calendar")
            ):
                enqueue_calendar(
                    connection,
                    run.source_id,
                    items,
                    self._notification_user_id,
                    completed_at,
                    previous_mapping,
                    run.mapping_hash,
                )
            if run.source is not None and run.source.allows_event_alert("meeting"):
                enqueue_meeting_reminders(
                    connection,
                    run,
                    items,
                    self._notification_user_id,
                    completed_at,
                    self._meeting_reminder_policy,
                )
            record_task_changes(
                connection,
                run,
                items,
                self._notification_user_id,
                completed_at,
                previous_mapping,
                self._task_change_policy,
            )
            if previous_mapping == run.mapping_hash:
                seed_converted_baselines(connection, run, self._notification_user_id)
            connection.executemany(
                "INSERT INTO sync_staging(run_id,item_id,payload,semantic_hash) VALUES(?,?,?,?)",
                staging_rows(run, items),
            )
            connection.execute(
                """INSERT INTO item_snapshots(source_id,item_id,payload,semantic_hash,mapping_hash,last_seen_run_id,observed_at,availability)
              SELECT ?,item_id,payload,semantic_hash,?,run_id,?,'present' FROM sync_staging WHERE run_id=?
              ON CONFLICT(source_id,item_id) DO UPDATE SET payload=excluded.payload,semantic_hash=excluded.semantic_hash,
              mapping_hash=excluded.mapping_hash,last_seen_run_id=excluded.last_seen_run_id,
              observed_at=excluded.observed_at,availability='present'""",
                (run.source_id, run.mapping_hash, completed_at, run.id),
            )
            # 처음 누락된 행만 전환하고, 계속 누락된 행은 마지막 정상 값과 상태를 보존한다.
            connection.execute(
                """UPDATE item_snapshots SET availability='unconfirmed_missing'
                WHERE source_id=? AND availability='present' AND last_seen_run_id<>?""",
                (run.source_id, run.id),
            )
            connection.execute(
                "UPDATE sync_runs SET status='succeeded',completed_at=? WHERE id=?",
                (completed_at, run.id),
            )
            connection.execute(
                "UPDATE work_source_state SET lease_until=NULL,mapping_hash=?,last_completed_at=? WHERE source_id=?",
                (run.mapping_hash, completed_at, run.source_id),
            )
            resolve_sync_failure(connection, run.source_id)
            record_field_alerts(connection, run, items, self._notification_user_id, completed_at)
            connection.execute("DELETE FROM sync_staging WHERE run_id=?", (run.id,))
            advance_schedule(connection, run.source_id, self._clock())

    async def fail_run(self, run: SyncRun, code: str, *, retry_after: float | None = None) -> None:
        """실패 기록도 작업 스레드의 짧은 트랜잭션으로 처리한다."""
        await asyncio.to_thread(self._fail_run, run, code, retry_after)

    def _fail_run(self, run: SyncRun, code: str, retry_after: float | None) -> None:
        """이전 실행의 실패가 새 세대의 lease를 해제하지 못하게 한다."""
        if code not in SOURCE_ERROR_CODES | {"source_failed", "storage_failed", "cancelled"}:
            raise SnapshotError("invalid_snapshot")
        with self._database.transaction() as connection:
            finished = self._clock()
            updated = connection.execute(
                "UPDATE sync_runs SET status='failed',failure_code=?,completed_at=? WHERE id=? AND source_id=? AND generation=? AND status='running'",
                (
                    "storage_failed" if code == "storage_failed" else "source_failed",
                    datetime.fromtimestamp(finished, UTC).isoformat(),
                    run.id,
                    run.source_id,
                    run.generation,
                ),
            )
            if updated.rowcount == 0:
                return  # 이전 세대의 뒤늦은 실패는 새 예약을 변경할 수 없다.
            retrying = fail_schedule(
                connection, run, code, finished, self._retry_policy, retry_after
            )
            # 종료·업데이트 취소는 실행권만 정리하고 장애 배너를 만들지 않는다.
            if code != "cancelled":
                for rule in saved_failure_rules(connection, run):
                    if rule.enabled and rule.target in (None, code):
                        enqueue_sync_failure(
                            connection, run, finished, code, retrying=retrying, rule=rule
                        )
                if run.source is not None and run.source.allows_event_alert("sync_error"):
                    enqueue_sync_failure(connection, run, finished, code, retrying=retrying)
            connection.execute(
                "UPDATE work_source_state SET lease_until=NULL WHERE source_id=? AND generation=?",
                (run.source_id, run.generation),
            )
