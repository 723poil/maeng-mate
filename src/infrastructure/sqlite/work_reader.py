"""같은 읽기 시점에서 현재 설정의 정상 스냅샷을 복원한다."""

import asyncio
import json
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime

from src.application.errors import QueryReadError, SnapshotError
from src.application.today_models import SourceSnapshot
from src.domain.models import SourceMapping
from src.infrastructure.sqlite.connection import Database
from src.infrastructure.sqlite.decoding import decode_item
from src.infrastructure.sqlite.serialization import fingerprint, source_fingerprint

ITEMS_QUERY = """
SELECT item_id, payload FROM item_snapshots
WHERE source_id=? AND mapping_hash=? AND availability='present'
ORDER BY item_id
"""


# 수집 시작·성공·실패와 다음 예약은 월 화면의 자료 또는 신선도를 바꾼다.
VERSION_QUERY = """
SELECT state.source_id, state.kind, state.generation, state.mapping_hash, {extra_columns}
       state.last_completed_at, run.status AS latest_run_status, schedule.next_run_at
FROM work_source_state AS state
LEFT JOIN sync_runs AS run
  ON run.source_id=state.source_id AND run.generation=state.generation
LEFT JOIN sync_schedules AS schedule ON schedule.source_id=state.source_id
WHERE state.source_id IN ({placeholders})
ORDER BY state.source_id
"""


@dataclass(frozen=True, slots=True)
class _SourceCopy:
    """동일 저장 버전·읽기 매핑에 한해 공유할 소스별 스냅샷이다."""

    version: tuple[object, ...]
    mapping: SourceMapping
    snapshot: SourceSnapshot


type _SourceCache = dict[str, tuple[_SourceCopy, ...]]


class SQLiteWorkReader:
    """복원본은 공유하되 요청한 소스와 칼럼만 순서대로 반환한다."""

    def __init__(self, database: Database, *, active_sources: Sequence[SourceMapping] = ()) -> None:
        """검색 범위가 좁아져도 현재 설정에 속하는 소스만 메모리에 유지한다."""
        self._database = database
        self._active_ids = frozenset(source.source_id for source in active_sources)
        self._cache_lock = asyncio.Lock()
        self._cache: _SourceCache = {}

    async def read_sources(self, sources: Sequence[SourceMapping]) -> tuple[SourceSnapshot, ...]:
        """같은 소스의 일반 조회와 메뉴바 전용 칼럼을 구별해 복원본을 재사용한다."""
        if not sources:
            return ()
        try:
            async with self._cache_lock:
                cache, result = await asyncio.to_thread(self._read_sources, tuple(sources))
                # 실패·취소된 작업 스레드가 이전 캐시를 덮지 않게 완료 후 교체한다.
                self._cache = cache
                return result
        except (SnapshotError, KeyError, TypeError, ValueError, OverflowError, OSError):
            raise QueryReadError() from None

    def _read_sources(
        self, sources: Sequence[SourceMapping]
    ) -> tuple[_SourceCache, tuple[SourceSnapshot, ...]]:
        """버전을 같은 트랜잭션에서 확인하고 변경되거나 처음 보는 소스만 읽는다."""
        cache = {key: copies for key, copies in self._cache.items() if key in self._active_ids}
        result = []
        with self._database.transaction(read_only=True) as connection:
            versions = source_versions(connection, sources)
            for source in sources:
                row = versions.get(source.source_id)
                version = tuple(row) if row is not None else ()
                current = tuple(
                    copy
                    for copy in self._cache.get(source.source_id, ())
                    if copy.version == version
                )
                match = next((copy for copy in current if copy.mapping == source), None)
                if match is None:
                    snapshot = self._decode_source(connection, source, row)
                    match = _SourceCopy(version, source, snapshot)
                    # 일반 조회와 숨긴 칼럼을 사용하는 메뉴바 조회, 최근 두 형태만 남긴다.
                    current = (*current[-1:], match)
                cache[source.source_id] = current
                result.append(match.snapshot)
        return cache, tuple(result)

    async def read_if_changed(
        self, sources: Sequence[SourceMapping], previous: str | None
    ) -> tuple[str, tuple[SourceSnapshot, ...]]:
        """같은 읽기 매핑에서 수집 버전이 달라졌을 때만 저장 JSON을 복원한다."""
        if not sources:
            return "", ()
        try:
            return await asyncio.to_thread(self._read_if_changed, sources, previous)
        except (SnapshotError, KeyError, TypeError, ValueError, OverflowError, OSError):
            raise QueryReadError() from None

    def _read_if_changed(
        self, sources: Sequence[SourceMapping], previous: str | None
    ) -> tuple[str, tuple[SourceSnapshot, ...]]:
        """버전과 원문을 한 읽기 시점에서 가져와 새 버전에 옛 자료를 연결하지 않는다."""
        with self._database.transaction(read_only=True) as connection:
            versions = source_versions(connection, sources, _COMPLETED_RUN_COLUMN)
            revision = fingerprint(
                [
                    (row["source_id"], row["kind"], row["mapping_hash"], row["completed_run_id"])
                    for row in versions.values()
                ]
            )
            if revision == previous:
                return revision, tuple(
                    snapshot_header(source, versions.get(source.source_id)) for source in sources
                )
            return revision, tuple(
                self._decode_source(connection, source, versions.get(source.source_id))
                for source in sources
            )

    def _decode_source(
        self, connection: sqlite3.Connection, source: SourceMapping, first: sqlite3.Row | None
    ) -> SourceSnapshot:
        """원본 JSON은 한 행씩 해석해 전체 원문과 복원본을 동시에 보유하지 않는다."""
        header = snapshot_header(source, first)
        if first is None or not header.mapping_matches:
            return header
        items = []
        for row in connection.execute(ITEMS_QUERY, (source.source_id, first["mapping_hash"])):
            payload = row["payload"]
            if not isinstance(payload, str):
                raise QueryReadError()
            value: object = json.loads(payload)
            item = decode_item(value, source)
            if item.id != row["item_id"]:
                raise QueryReadError()
            items.append(item)
        options = None
        if source.kind == "task" and "status" in source.properties:
            row = connection.execute(
                "SELECT options_json FROM source_status_options WHERE source_id=?",
                (source.source_id,),
            ).fetchone()
            if row is not None:
                value = json.loads(row["options_json"])
                if not isinstance(value, list) or not all(
                    isinstance(entry, str) for entry in value
                ):
                    raise QueryReadError()
                options = tuple(value)
        return replace(header, items=tuple(items), status_options=options)


# 성공한 실행 ID는 완료 시각이 같아도 스냅샷 교체를 구분한다.
_COMPLETED_RUN_COLUMN = """(SELECT id FROM sync_runs
        WHERE source_id=state.source_id AND status='succeeded'
        ORDER BY generation DESC LIMIT 1) AS completed_run_id,"""


def source_versions(
    connection: sqlite3.Connection, sources: Sequence[SourceMapping], extra_columns: str = ""
) -> dict[str, sqlite3.Row]:
    """요청한 소스의 작은 메타데이터만 조회하고 잘못된 중복 입력을 거부한다."""
    identifiers = tuple(source.source_id for source in sources)
    if len(set(identifiers)) != len(identifiers):
        raise QueryReadError()
    placeholders = ",".join("?" for _ in identifiers)
    return {
        row["source_id"]: row
        for row in connection.execute(
            VERSION_QUERY.format(placeholders=placeholders, extra_columns=extra_columns),
            identifiers,
        )
    }


def snapshot_header(source: SourceMapping, first: sqlite3.Row | None) -> SourceSnapshot:
    """공통 수집 버전에서 매핑 일치·시각·예약만 확인하고 원본은 읽지 않는다."""
    if first is None:
        return SourceSnapshot(source.source_id, source.kind, None, None, False, ())
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
    matches = first["kind"] == source.kind and first["mapping_hash"] == source_fingerprint(source)
    next_epoch = first["next_run_at"]
    if next_epoch is not None and type(next_epoch) not in (int, float):
        raise QueryReadError()
    next_at = (
        datetime.fromtimestamp(next_epoch, UTC)
        if isinstance(next_epoch, (int, float)) and next_epoch > 0
        else None
    )
    return SourceSnapshot(source.source_id, source.kind, completed_at, status, matches, (), next_at)
