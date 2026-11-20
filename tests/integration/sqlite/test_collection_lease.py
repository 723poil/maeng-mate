"""임시 SQLite와 지연 원천으로 장시간 수집의 실행권 수명을 확인한다."""

import asyncio
from dataclasses import replace
from importlib import import_module

import pytest

from src.application.errors import SnapshotError, SourceReadError
from src.application.use_cases.synchronize import synchronize
from src.domain.models import SourceMapping, WorkItem
from src.infrastructure.notion.normalization import normalize_page
from src.infrastructure.sqlite.connection import Database
from src.infrastructure.sqlite.schedules import SQLiteSchedules
from src.infrastructure.sqlite.snapshots import SQLiteSnapshots
from tests.fixtures.clock import Clock
from tests.fixtures.collection import CollectionSource
from tests.fixtures.notion import OBSERVED, SOURCE, page


@pytest.mark.parametrize("scheduled", [False, True])
@pytest.mark.parametrize("partial_failure", [False, True])
async def test_long_collection_preserves_lease_and_snapshot(
    db: Database, monkeypatch: pytest.MonkeyPatch, scheduled: bool, partial_failure: bool
) -> None:
    """5분을 넘겨도 성공을 확정하고 부분 실패에서는 마지막 정상 행을 보존한다."""
    clock = Clock()
    schedules = SQLiteSchedules(db, clock=clock)
    await schedules.configure((SOURCE,), 300)
    store = SQLiteSnapshots(db, scheduled=scheduled, clock=clock)
    item = normalize_page(page(), SOURCE, OBSERVED)
    initial = await store.begin_run(SOURCE, clock.now().isoformat())
    await store.complete_run(initial, [item], clock.now().isoformat())
    clock.value += 300
    # 실제 5분을 기다리지 않고 원천 대기 중 갱신과 경쟁 획득을 교차시킨다.
    monkeypatch.setattr(
        import_module("src.application.use_cases.synchronize"),
        "LEASE_RENEWAL_INTERVAL_SECONDS",
        0.005,
    )
    before = await db.fetch("SELECT * FROM item_snapshots")

    async def collect(source: SourceMapping, observed_at: str) -> list[WorkItem]:
        """여러 실행권 기간 동안 경쟁 실행을 막고 새 행을 반환한다."""
        for _ in range(8):
            clock.value += 100

            async def renewed() -> None:
                """실제 갱신 결과를 읽어 다음 시계 이동과 순서를 맞춘다."""
                while True:
                    state = await db.fetch("SELECT lease_until FROM work_source_state")
                    lease = state[0]["lease_until"]
                    if isinstance(lease, (float, int)) and lease >= clock.value + 300:
                        return
                    await asyncio.sleep(0)

            await asyncio.wait_for(renewed(), 2)
            with pytest.raises(SnapshotError, match="source_busy"):
                await SQLiteSnapshots(db, clock=clock).begin_run(source, clock.now().isoformat())
        if partial_failure:
            raise SourceReadError("unavailable")
        return [replace(item, title="renewed")]

    result = await synchronize(SOURCE, CollectionSource(collect), store, clock.now)
    if partial_failure:
        assert result == {"status": "failed", "code": "source_failed", "reason": "unavailable"}
        assert await db.fetch("SELECT * FROM item_snapshots") == before
    else:
        assert result == {"status": "succeeded", "count": 1}
        rows = await db.fetch("SELECT payload,availability FROM item_snapshots")
        assert "renewed" in str(rows[0]["payload"])
        assert rows[0]["availability"] == "present"
    assert await db.fetch("SELECT lease_until FROM work_source_state") == [{"lease_until": None}]
    assert await db.fetch("SELECT * FROM sync_staging") == []


async def test_stopped_renewal_allows_takeover_and_fences_old_run(db: Database) -> None:
    """갱신이 중단되면 만료 시 인계하고 이전 결과·갱신·실패를 차단한다."""
    clock = Clock()
    store = SQLiteSnapshots(db, clock=clock)
    first = await store.begin_run(SOURCE, clock.now().isoformat())
    clock.value += 299
    await store.renew_run(first)
    clock.value += 299
    with pytest.raises(SnapshotError, match="source_busy"):
        await store.begin_run(SOURCE, clock.now().isoformat())
    clock.value += 1
    # 만료 경계에서도 아직 새 세대가 없다는 이유로 실행권을 부활시키지 않는다.
    with pytest.raises(SnapshotError, match="stale_run"):
        await store.renew_run(first)
    second = await store.begin_run(SOURCE, clock.now().isoformat())
    assert second.generation == first.generation + 1
    before = await db.fetch("SELECT * FROM work_source_state")
    with pytest.raises(SnapshotError, match="stale_run"):
        await store.renew_run(first)
    with pytest.raises(SnapshotError, match="stale_run"):
        await store.complete_run(first, [], clock.now().isoformat())
    await store.fail_run(first, "source_failed")
    assert await db.fetch("SELECT * FROM work_source_state") == before
    await store.complete_run(second, [], clock.now().isoformat())
    with pytest.raises(SnapshotError, match="stale_run"):
        await store.renew_run(second)
    assert await db.fetch("SELECT status FROM sync_runs ORDER BY id") == [
        {"status": "abandoned"},
        {"status": "succeeded"},
    ]


@pytest.mark.parametrize("failure", ["storage_failed", "stale_run", "cancelled"])
async def test_renewal_failure_or_cancellation_drains_source(
    db: Database, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    """갱신 오류와 취소 뒤 원천 작업을 회수하고 새 세대와 정상 스냅샷을 지킨다."""
    clock = Clock()
    store = SQLiteSnapshots(db, clock=clock)
    item = normalize_page(page(), SOURCE, OBSERVED)
    await store.complete_run(
        await store.begin_run(SOURCE, clock.now().isoformat()), [item], clock.now().isoformat()
    )
    before = await db.fetch("SELECT * FROM item_snapshots")
    monkeypatch.setattr(
        import_module("src.application.use_cases.synchronize"),
        "LEASE_RENEWAL_INTERVAL_SECONDS",
        0.005,
    )
    entered, closed = asyncio.Event(), asyncio.Event()

    async def collect(source: SourceMapping, observed_at: str) -> list[WorkItem]:
        """경쟁 인계나 DB 쓰기 실패 뒤 정리 여부를 알린다."""
        try:
            if failure == "storage_failed":
                with db.transaction() as connection:
                    connection.execute(
                        """CREATE TRIGGER reject_renewal BEFORE UPDATE ON work_source_state
                        WHEN NEW.lease_until IS NOT NULL AND NEW.generation=OLD.generation
                        BEGIN SELECT RAISE(ABORT, 'synthetic renewal failure'); END"""
                    )
            elif failure == "stale_run":
                clock.value += 301
                await SQLiteSnapshots(db, clock=clock).begin_run(source, clock.now().isoformat())
            entered.set()
            await asyncio.Event().wait()
            return [replace(item, title="must not publish")]
        finally:
            closed.set()

    task = asyncio.create_task(synchronize(SOURCE, CollectionSource(collect), store, clock.now))
    await asyncio.wait_for(entered.wait(), 2)
    if failure == "cancelled":
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    else:
        assert await asyncio.wait_for(task, 2) == {"status": "failed", "code": failure}
    assert closed.is_set()
    assert await db.fetch("SELECT * FROM item_snapshots") == before
    assert await db.fetch("SELECT * FROM sync_staging") == []
    state = await db.fetch("SELECT generation,lease_until FROM work_source_state")
    if failure == "stale_run":
        assert state == [{"generation": 3, "lease_until": clock.value + 300}]
    else:
        assert state == [{"generation": 2, "lease_until": None}]
