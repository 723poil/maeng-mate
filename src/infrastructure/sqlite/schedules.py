"""소스별 정기 수집 예약을 SQLite에 보존한다."""

import asyncio
import sqlite3
import time
from collections.abc import Callable, Sequence
from datetime import UTC, datetime

from src.application.errors import SOURCE_ERROR_CODES, SnapshotError
from src.application.models import SyncRun
from src.domain.models import SourceMapping
from src.domain.retry import RetryPolicy
from src.infrastructure.sqlite.connection import Database
from src.infrastructure.sqlite.serialization import collection_fingerprint

# 실행 시각은 예약과 실행권으로 결정하고, 상태 집계에서만 최신 실행 이력을 연결한다.
SCHEDULE_TIMES = """
    SELECT schedule.source_id, schedule.next_run_at, state.lease_until,
           schedule.retry_pending, schedule.retry_count
    FROM sync_schedules AS schedule
    JOIN work_source_state AS state ON state.source_id=schedule.source_id
"""

SCHEDULE_STATUS = """
    SELECT schedule.source_id, schedule.interval_seconds, schedule.next_run_at, schedule.retry_count,
           schedule.retry_pending, schedule.error_code, state.lease_until, run.status
    FROM sync_schedules AS schedule
    JOIN work_source_state AS state ON state.source_id=schedule.source_id
    LEFT JOIN sync_runs AS run ON run.source_id=state.source_id AND run.generation=state.generation
"""


class SQLiteSchedules:
    """현재 설정 소스의 예약만 등록하고 집계한다."""

    def __init__(self, database: Database, *, clock: Callable[[], float] = time.time) -> None:
        """예약과 lease 비교에 같은 시계 경계를 사용한다."""
        self._database = database
        self._clock = clock

    async def configure(self, sources: Sequence[SourceMapping], interval: int) -> None:
        """명시적으로 준비된 DB에만 현재 주기를 반영한다."""
        if interval < 60:
            raise ValueError("수집 간격은 60초 이상이어야 합니다.")
        await asyncio.to_thread(self._configure, sources, interval)

    def _configure(self, sources: Sequence[SourceMapping], interval: int) -> None:
        """간격이 바뀐 경우에만 마지막 종료를 기준으로 예약을 조정한다."""
        with self._database.transaction() as connection:
            # 화면에서 저장한 값은 시작 환경의 기본 주기보다 우선한다.
            preference = connection.execute(
                "SELECT interval_seconds FROM collection_preferences WHERE id=1"
            ).fetchone()
            if preference is not None:
                interval = int(preference["interval_seconds"])
            for source in sources:
                connection.execute(
                    "INSERT INTO work_source_state(source_id,kind) VALUES(?,?) ON CONFLICT DO NOTHING",
                    (source.source_id, source.kind),
                )
                connection.execute(
                    """INSERT INTO sync_schedules(source_id,interval_seconds,next_run_at,mapping_hash)
                    VALUES(?,?,0,?) ON CONFLICT(source_id) DO UPDATE SET
                    interval_seconds=excluded.interval_seconds,
                    next_run_at=CASE WHEN sync_schedules.mapping_hash IS NOT excluded.mapping_hash THEN 0
                        WHEN sync_schedules.retry_pending=1 THEN sync_schedules.next_run_at
                        ELSE COALESCE(sync_schedules.last_finished_at+excluded.interval_seconds,0) END,
                    retry_count=CASE WHEN sync_schedules.mapping_hash IS NOT excluded.mapping_hash
                        THEN 0 ELSE sync_schedules.retry_count END,
                    retry_pending=CASE WHEN sync_schedules.mapping_hash IS NOT excluded.mapping_hash
                        THEN 0 ELSE sync_schedules.retry_pending END,
                    error_code=CASE WHEN sync_schedules.mapping_hash IS NOT excluded.mapping_hash
                        THEN NULL ELSE sync_schedules.error_code END,
                    mapping_hash=excluded.mapping_hash
                    WHERE sync_schedules.interval_seconds<>excluded.interval_seconds
                        OR sync_schedules.mapping_hash IS NOT excluded.mapping_hash""",
                    (source.source_id, interval, collection_fingerprint(source)),
                )

    async def set_interval(self, sources: Sequence[SourceMapping], seconds: int) -> None:
        """선호값과 모든 현재 소스의 다음 예약을 한 번에 저장한다."""
        if type(seconds) is not int or not 60 <= seconds <= 86400:
            raise ValueError("수집 주기는 60~86400초여야 합니다.")
        await asyncio.to_thread(self._set_interval, sources, seconds)

    def _set_interval(self, sources: Sequence[SourceMapping], seconds: int) -> None:
        """진행 중 lease는 유지하고 변경 시점부터 새 간격으로 예약한다."""
        with self._database.transaction() as connection:
            connection.execute(
                """INSERT INTO collection_preferences(id,interval_seconds) VALUES(1,?)
                ON CONFLICT(id) DO UPDATE SET interval_seconds=excluded.interval_seconds""",
                (seconds,),
            )
            for source in sources:
                connection.execute(
                    """UPDATE sync_schedules SET interval_seconds=?,next_run_at=
                    CASE WHEN retry_pending=1 THEN next_run_at ELSE ? END WHERE source_id=?""",
                    (seconds, self._clock() + seconds, source.source_id),
                )

    async def _rows(self, query: str, sources: Sequence[SourceMapping]) -> list[dict[str, object]]:
        """두 조회 모두 현재 설정의 소스 ID만 바인딩한다."""
        if not sources:
            return []
        placeholders = ",".join("?" for _ in sources)
        return await self._database.fetch(
            query + f" WHERE schedule.source_id IN ({placeholders})",
            [source.source_id for source in sources],
        )

    async def due_sources_and_delay(
        self, sources: Sequence[SourceMapping]
    ) -> tuple[tuple[SourceMapping, ...], float]:
        """실행권이 비고 기한이 된 소스만 설정 순서로 돌려준다."""
        rows = await self._rows(SCHEDULE_TIMES, sources)
        now = self._clock()
        due_at = collection_due_times(rows, sources)
        # 초기 설정에서 등록한 예약이 사라졌다면 유휴 상태로 처리할 수 없다.
        try:
            due = tuple(source for source in sources if due_at[source.source_id] <= now)
        except KeyError:
            raise SnapshotError("storage_failed") from None
        delay = max(0.0, min(due_at.values()) - now) if due_at else 60.0
        return due, delay

    async def read_status(self, sources: Sequence[SourceMapping]) -> dict[str, object]:
        """인증 응답에 필요한 집계만 제공하고 식별자와 원문은 제외한다."""
        rows = await self._rows(SCHEDULE_STATUS, sources)
        due = min(collection_due_times(rows, sources).values(), default=None)
        return {
            "retryingSources": sum(row["retry_pending"] == 1 for row in rows),
            "maxRetryAttempt": max(
                (stored_retry_count(row) for row in rows if row["retry_pending"] == 1), default=0
            ),
            "failureReasons": sorted(
                {failure_reason(row["error_code"]) for row in rows if row["error_code"] is not None}
            ),
            "intervalSeconds": rows[0]["interval_seconds"] if rows else None,
            "nextRunAt": datetime.fromtimestamp(max(self._clock(), due), UTC).isoformat()
            if due is not None
            else None,
            "succeededSources": sum(row["status"] == "succeeded" for row in rows),
            "failedSources": sum(row["status"] == "failed" for row in rows),
            "runningSources": sum(row["status"] == "running" for row in rows),
            "configuredSources": len(sources),
        }


def stored_retry_count(row: dict[str, object]) -> int:
    """외부 응답에서 숫자 문자열을 재시도 횟수로 가장하지 않는다."""
    value = row["retry_count"]
    if type(value) is not int or not 0 <= value <= 3:
        raise ValueError("재시도 저장값을 확인하세요.")
    return value


def failure_reason(value: object) -> str:
    """상태 응답에는 검증한 고정 오류 코드만 노출한다."""
    return (
        value
        if isinstance(value, str)
        and value in SOURCE_ERROR_CODES | {"source_failed", "storage_failed"}
        else "unknown"
    )


def collection_due_times(
    rows: Sequence[dict[str, object]], sources: Sequence[SourceMapping]
) -> dict[str, float]:
    """동일 원본과 재시도 단계의 연결이 한 번에 실행되도록 예약 시각을 맞춘다."""
    due_at: dict[str, float] = {}
    by_id = {source.source_id: source for source in sources}
    groups: dict[tuple[object, ...], float] = {}
    memberships: dict[str, tuple[object, ...]] = {}
    for row in rows:
        identifier = row["source_id"]
        if not isinstance(identifier, str):
            raise SnapshotError("storage_failed")
        due_at[identifier] = effective_due(row)
        source = by_id[identifier]
        # 즉시 재수집할 새 매핑과 실행 중인 소스는 다른 연결의 예약을 미루지 않는다.
        if row["next_run_at"] == 0 or effective_due(row) != row["next_run_at"]:
            continue
        # 같은 재시도 단계만 묶고 가장 늦은 시각을 지켜 서버의 대기 지시도 보존한다.
        key = (source.provider, source.origin, row["retry_pending"], row["retry_count"])
        memberships[identifier] = key
        groups[key] = max(groups.get(key, 0), due_at[identifier])
    due_at.update({identifier: groups[key] for identifier, key in memberships.items()})
    return due_at


def effective_due(row: dict[str, object]) -> float:
    """UTC epoch 예약과 유효 lease 중 늦은 시각을 사용한다."""
    due, lease = row["next_run_at"], row["lease_until"]
    if not isinstance(due, (float, int)) or (
        lease is not None and not isinstance(lease, (float, int))
    ):
        raise ValueError("예약 저장값을 확인하세요.")
    return max(float(due), float(lease) if lease is not None else 0.0)


def advance_schedule(connection: sqlite3.Connection, source_id: str, finished_at: float) -> None:
    """스냅샷 확정·실패 트랜잭션 안에서만 다음 예약을 갱신한다."""
    connection.execute(
        """UPDATE sync_schedules SET last_finished_at=?,next_run_at=?+interval_seconds,
        retry_count=0,retry_pending=0,error_code=NULL WHERE source_id=?""",
        (finished_at, finished_at, source_id),
    )


def fail_schedule(
    connection: sqlite3.Connection,
    run: SyncRun,
    code: str,
    finished_at: float,
    policy: RetryPolicy | None,
    retry_after: float | None,
) -> bool:
    """실패 기록과 같은 트랜잭션에서 다음 재시도 또는 정기 예약을 결정한다."""
    row = connection.execute(
        "SELECT retry_count,error_code FROM sync_schedules WHERE source_id=?", (run.source_id,)
    ).fetchone()
    if row is None:
        return False
    count = int(row["retry_count"])
    if code == "cancelled":
        # 중단된 빠른 재시도는 예산을 더 소모하지 않고 재실행 뒤 같은 차례로 이어간다.
        if policy is not None and count > 0 and isinstance(row["error_code"], str):
            delay = policy.delay(row["error_code"], count - 1, None)
            if delay is not None:
                connection.execute(
                    "UPDATE sync_schedules SET retry_pending=1,next_run_at=? WHERE source_id=?",
                    (finished_at + delay, run.source_id),
                )
                return True
        advance_schedule(connection, run.source_id, finished_at)
        return False
    delay = policy.delay(code, count, retry_after) if policy is not None else None
    if delay is not None:
        connection.execute(
            """UPDATE sync_schedules SET last_finished_at=?,next_run_at=?,retry_count=?,
            retry_pending=1,error_code=? WHERE source_id=?""",
            (finished_at, finished_at + delay, count + 1, code, run.source_id),
        )
        return True
    connection.execute(
        """UPDATE sync_schedules SET last_finished_at=?,next_run_at=?+interval_seconds,
        retry_count=0,retry_pending=0,error_code=? WHERE source_id=?""",
        (finished_at, finished_at, code, run.source_id),
    )
    return False
