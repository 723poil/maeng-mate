"""현재 설정의 정상 스냅샷을 한 읽기 전용 SQL로 조회한다."""

import asyncio
import json
from collections.abc import Sequence
from datetime import UTC, datetime

from src.application.errors import QueryReadError, SnapshotError
from src.application.today_models import SourceSnapshot
from src.domain.models import SourceMapping
from src.infrastructure.sqlite.connection import Database
from src.infrastructure.sqlite.decoding import decode_item
from src.infrastructure.sqlite.serialization import fingerprint, source_fingerprint

QUERY = """
SELECT state.source_id, state.kind, state.mapping_hash, state.last_completed_at,
       run.status AS latest_run_status, snapshot.item_id, snapshot.payload, schedule.next_run_at
FROM work_source_state AS state
LEFT JOIN sync_runs AS run
  ON run.source_id = state.source_id AND run.generation = state.generation
LEFT JOIN sync_schedules AS schedule ON schedule.source_id=state.source_id
LEFT JOIN item_snapshots AS snapshot
  ON snapshot.source_id = state.source_id AND snapshot.availability = 'present'
  AND snapshot.mapping_hash = state.mapping_hash
WHERE state.source_id IN ({placeholders})
ORDER BY state.source_id, snapshot.item_id
"""


# 수집 시작·성공·실패와 다음 예약은 월 화면의 자료 또는 신선도를 바꾼다.
VERSION_QUERY = """
SELECT state.source_id, state.kind, state.generation, state.mapping_hash,
       state.last_completed_at, run.status, schedule.next_run_at
FROM work_source_state AS state
LEFT JOIN sync_runs AS run
  ON run.source_id=state.source_id AND run.generation=state.generation
LEFT JOIN sync_schedules AS schedule ON schedule.source_id=state.source_id
WHERE state.source_id IN ({placeholders})
ORDER BY state.source_id
"""


class SQLiteWorkReader:
    """조회만 제공하며 실행 기록과 정상 스냅샷을 진행시키지 않는다."""

    def __init__(self, database: Database) -> None:
        """저장 구현과 같은 DB 연결 경계만 주입받는다."""
        self._database = database

        self._cache_lock = asyncio.Lock()
        self._cached_sources: tuple[SourceMapping, ...] = ()
        self._cached_revision: str | None = None
        self._cached_result: tuple[SourceSnapshot, ...] = ()

    async def read_sources(self, sources: Sequence[SourceMapping]) -> tuple[SourceSnapshot, ...]:
        """같은 소스·칼럼·수집 버전이면 마지막으로 복원한 스냅샷을 재사용한다."""
        selected = tuple(sources)
        if not selected:
            return ()
        async with self._cache_lock:
            # 같은 ID여도 숨긴 칼럼 등 읽기 매핑이 다르면 이전 데이터를 사용할 수 없다.
            previous = self._cached_revision if selected == self._cached_sources else None
            revision, snapshots = await self.read_if_changed(selected, previous)
            if snapshots is not None:
                self._cached_sources = selected
                self._cached_revision = revision
                self._cached_result = snapshots
            return self._cached_result

    async def read_if_changed(
        self, sources: Sequence[SourceMapping], previous: str | None
    ) -> tuple[str, tuple[SourceSnapshot, ...] | None]:
        """같은 읽기 매핑에서 수집 버전이 달라졌을 때만 저장 JSON을 복원한다."""
        if not sources:
            return "", () if previous != "" else None
        try:
            return await asyncio.to_thread(self._read_if_changed, sources, previous)
        except (SnapshotError, KeyError, TypeError, ValueError, OverflowError, OSError):
            raise QueryReadError() from None

    def _read_if_changed(
        self, sources: Sequence[SourceMapping], previous: str | None
    ) -> tuple[str, tuple[SourceSnapshot, ...] | None]:
        """버전과 원문을 한 읽기 시점에서 가져와 새 버전에 옛 자료를 연결하지 않는다."""
        identifiers = tuple(source.source_id for source in sources)
        if len(set(identifiers)) != len(identifiers):
            raise QueryReadError()
        placeholders = ",".join("?" for _ in identifiers)
        with self._database.transaction(read_only=True) as connection:
            versions = connection.execute(
                VERSION_QUERY.format(placeholders=placeholders), identifiers
            )
            revision = fingerprint([tuple(row) for row in versions])
            if revision == previous:
                return revision, None
            rows = [
                dict(row)
                for row in connection.execute(QUERY.format(placeholders=placeholders), identifiers)
            ]
        return revision, self._snapshots(sources, rows)

    def _snapshots(
        self, sources: Sequence[SourceMapping], rows: list[dict[str, object]]
    ) -> tuple[SourceSnapshot, ...]:
        """소스별 저장 행을 모아 설정 순서대로 복원한다."""
        grouped: dict[str, list[dict[str, object]]] = {source.source_id: [] for source in sources}
        for row in rows:
            identifier = row["source_id"]
            if not isinstance(identifier, str) or identifier not in grouped:
                raise QueryReadError()
            grouped[identifier].append(row)
        return tuple(self._decode_source(source, grouped[source.source_id]) for source in sources)

    def _decode_source(
        self, source: SourceMapping, rows: list[dict[str, object]]
    ) -> SourceSnapshot:
        """기존 매핑의 자료를 새 매핑에 연결하지 않는다."""
        if not rows:
            return SourceSnapshot(source.source_id, source.kind, None, None, False, ())
        first = rows[0]
        stored_time = first["last_completed_at"]
        completed_at = None
        if stored_time is not None:
            if not isinstance(stored_time, str):
                raise QueryReadError()
            completed_at = datetime.fromisoformat(stored_time)
        if completed_at is not None and (
            not isinstance(completed_at, datetime) or completed_at.tzinfo is None
        ):
            raise QueryReadError()
        status = first["latest_run_status"]
        if status not in {None, "running", "succeeded", "failed", "abandoned"}:
            raise QueryReadError()
        if status is not None and not isinstance(status, str):
            raise QueryReadError()
        matches = first["kind"] == source.kind and first["mapping_hash"] == source_fingerprint(
            source
        )
        items = []
        if matches:
            for row in rows:
                if row["item_id"] is not None:
                    payload = row["payload"]
                    if not isinstance(payload, str):
                        raise QueryReadError()
                    value: object = json.loads(payload)
                    item = decode_item(value, source)
                    if item.id != row["item_id"]:
                        raise QueryReadError()
                    items.append(item)
        next_epoch = first["next_run_at"]
        if next_epoch is not None and type(next_epoch) not in (int, float):
            raise QueryReadError()
        next_at = (
            datetime.fromtimestamp(next_epoch, UTC)
            if isinstance(next_epoch, (int, float)) and next_epoch > 0
            else None
        )
        return SourceSnapshot(
            source.source_id, source.kind, completed_at, status, matches, tuple(items), next_at
        )
