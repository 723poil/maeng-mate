"""예약과 스냅샷이 같은 SQLite 실행권을 사용하는지 검증한다."""

import asyncio
from dataclasses import replace

import pytest

from src.application.errors import SnapshotError
from src.application.periodic import PeriodicCollector
from src.application.use_cases.synchronize import synchronize
from src.domain.models import SourceMapping, WorkItem
from src.infrastructure.notion.normalization import normalize_page
from src.infrastructure.sqlite.connection import Database
from src.infrastructure.sqlite.schedules import SQLiteSchedules
from src.infrastructure.sqlite.snapshots import SQLiteSnapshots
from tests.fixtures.clock import Clock
from tests.fixtures.notion import OBSERVED, SOURCE, page


async def configure(db: Database, clock: Clock, interval: int = 300) -> SQLiteSchedules:
    """실제 예약 저장소를 준비하며 없는 기능은 명확하게 실패시킨다."""
    store = SQLiteSchedules(db, clock=clock)
    await store.configure((SOURCE,), interval)
    return store


@pytest.mark.parametrize("expired_after", [300, 301])
async def test_status_counts_only_current_unexpired_lease(db: Database, expired_after: int) -> None:
    """만료 경계와 다음 세대의 진행 집계를 확인하며 조회의 저장 부작용을 막는다."""
    clock = Clock()
    schedules = await configure(db, clock)
    store = SQLiteSnapshots(db, scheduled=True, clock=clock)
    first = await store.begin_run(SOURCE, clock.now().isoformat())
    clock.value += 299
    assert (await schedules.read_status((SOURCE,)))["runningSources"] == 1
    assert await schedules.due_sources_and_delay((SOURCE,)) == ((), 1)

    clock.value += expired_after - 299
    tables = ("sync_runs", "work_source_state", "sync_schedules")
    before = {table: await db.fetch(f"SELECT * FROM {table}") for table in tables}
    status = await schedules.read_status((SOURCE,))
    assert await schedules.due_sources_and_delay((SOURCE,)) == ((SOURCE,), 0)
    assert {table: await db.fetch(f"SELECT * FROM {table}") for table in tables} == before
    # 조회는 만료 이력을 실패로 바꾸지 않고 실제 유효 실행권만 진행 중으로 센다.
    assert status["runningSources"] == 0
    assert status["failedSources"] == 0
    assert status["succeededSources"] == 0
    assert status["nextRunAt"] == clock.now().isoformat()

    second = await store.begin_run(SOURCE, clock.now().isoformat())
    assert second.generation == first.generation + 1
    assert (await schedules.read_status((SOURCE,)))["runningSources"] == 1
    assert await schedules.due_sources_and_delay((SOURCE,)) == ((), 300)
    assert await db.fetch("SELECT status FROM sync_runs ORDER BY id") == [
        {"status": "abandoned"},
        {"status": "running"},
    ]
    await store.complete_run(second, [], clock.now().isoformat())
    status = await schedules.read_status((SOURCE,))
    assert status["runningSources"] == 0
    assert status["succeededSources"] == 1
    clock.value += 300
    third = await store.begin_run(SOURCE, clock.now().isoformat())
    await store.fail_run(third, "source_failed")
    status = await schedules.read_status((SOURCE,))
    assert status["runningSources"] == 0
    assert status["failedSources"] == 1


async def test_idle_collector_can_wait_while_another_writer_holds_db(db: Database) -> None:
    """예약까지 대기하는 실행기는 다른 쓰기 트랜잭션의 종료를 기다리지 않는다."""
    clock = Clock()
    schedules = SQLiteSchedules(db, clock=clock)
    await schedules.configure((SOURCE,), 300)
    await schedules.set_interval((SOURCE,), 120)
    waits: list[float] = []

    async def wait(stop: asyncio.Event, delay: float) -> None:
        """예약 시각을 기록하고 한 번의 확인으로 종료한다."""
        waits.append(delay)
        stop.set()

    class IdleSource:
        """기한 전에 외부 요청이 발생하면 검증을 실패시킨다."""

        async def collect(self, source: SourceMapping, observed_at: str) -> list[WorkItem]:
            """유휴 상태에서는 수집할 수 없다."""
            pytest.fail("기한 전 수집")

    collector = PeriodicCollector(
        (SOURCE,),
        IdleSource(),
        SQLiteSnapshots(db, scheduled=True, clock=clock),
        schedules,
        now=clock.now,
        wait=wait,
    )
    with db.transaction():
        await asyncio.wait_for(collector.run(asyncio.Event()), timeout=2)
    assert waits == [120]
    assert await db.fetch("SELECT id FROM sync_runs") == []


async def test_schedule_survives_restart_and_skips_missed_ticks(db: Database) -> None:
    """기한 전 재실행을 막고 절전 후에도 한 번만 실행한다."""
    clock = Clock()
    await configure(db, clock)
    store = SQLiteSnapshots(db, scheduled=True, clock=clock)
    first = await store.begin_run(SOURCE, clock.now().isoformat())
    clock.value += 10
    await store.complete_run(first, [], clock.now().isoformat())
    restarted = SQLiteSchedules(db, clock=clock)
    await restarted.configure((SOURCE,), 300)
    assert await restarted.due_sources_and_delay((SOURCE,)) == ((), 300)
    with pytest.raises(SnapshotError, match="source_not_due"):
        await store.begin_run(SOURCE, clock.now().isoformat())
    clock.value += 3600
    second = await store.begin_run(SOURCE, clock.now().isoformat())
    await store.complete_run(second, [], clock.now().isoformat())
    assert await restarted.due_sources_and_delay((SOURCE,)) == ((), 300)
    assert len(await db.fetch("SELECT id FROM sync_runs")) == 2


async def test_schedule_concurrency_manual_sync_and_old_generation(db: Database) -> None:
    """수동 실행·경쟁자·이전 실행 실패가 현재 예약을 훼손하지 않는다."""
    clock = Clock()
    await configure(db, clock)
    scheduled = SQLiteSnapshots(db, scheduled=True, clock=clock)
    manual = SQLiteSnapshots(db, clock=clock)
    results = await asyncio.gather(
        scheduled.begin_run(SOURCE, OBSERVED),
        manual.begin_run(SOURCE, OBSERVED),
        return_exceptions=True,
    )
    assert sum(isinstance(result, SnapshotError) for result in results) == 1
    current = await db.fetch("SELECT generation FROM work_source_state")
    assert current == [{"generation": 1}]
    # 만료된 실행만 다음 세대가 대체할 수 있다.
    clock.value += 301
    second = await scheduled.begin_run(SOURCE, OBSERVED)
    first = next(result for result in results if not isinstance(result, BaseException))
    await manual.fail_run(first, "source_failed")
    await scheduled.complete_run(second, [], clock.now().isoformat())
    assert await SQLiteSchedules(db, clock=clock).due_sources_and_delay((SOURCE,)) == ((), 300)


async def test_failure_and_interval_changes_are_persisted(db: Database) -> None:
    """실패에도 다음 주기를 기록하고 변경 간격은 마지막 종료를 기준으로 적용한다."""
    clock = Clock()
    await configure(db, clock)
    store = SQLiteSnapshots(db, scheduled=True, clock=clock)
    run = await store.begin_run(SOURCE, OBSERVED)
    await store.fail_run(run, "source_failed")
    schedules = SQLiteSchedules(db, clock=clock)
    assert await schedules.due_sources_and_delay((SOURCE,)) == ((), 300)
    await schedules.configure((SOURCE,), 60)
    assert await schedules.due_sources_and_delay((SOURCE,)) == ((), 60)
    status = await schedules.read_status((SOURCE,))
    assert status["failedSources"] == 1
    assert SOURCE.source_id not in str(status)
    assert (await db.fetch("SELECT completed_at FROM sync_runs"))[0]["completed_at"] is not None


async def test_schedule_update_failure_rolls_back_snapshot(db: Database) -> None:
    """다음 예약 갱신 실패도 정상 스냅샷과 실행 상태까지 롤백한다."""
    clock = Clock()
    await configure(db, clock)
    store = SQLiteSnapshots(db, scheduled=True, clock=clock)
    item = normalize_page(page(), SOURCE, OBSERVED)
    first = await store.begin_run(SOURCE, OBSERVED)
    await store.complete_run(first, [item], clock.now().isoformat())
    clock.value += 300
    second = await store.begin_run(SOURCE, OBSERVED)
    with db.transaction() as connection:
        connection.execute(
            "CREATE TRIGGER reject_schedule BEFORE UPDATE ON sync_schedules BEGIN SELECT RAISE(ABORT, 'fake'); END"
        )
    with pytest.raises(SnapshotError):
        await store.complete_run(second, [replace(item, id="new")], clock.now().isoformat())
    assert await db.fetch("SELECT item_id FROM item_snapshots") == [{"item_id": item.id}]
    assert (await db.fetch("SELECT status FROM sync_runs WHERE id=?", (second.id,)))[0][
        "status"
    ] == "running"


async def test_cancelled_collection_releases_lease_without_publishing(db: Database) -> None:
    """종료 취소가 실행 중 상태나 부분 스냅샷을 남기지 않는다."""
    clock = Clock()
    await configure(db, clock)
    entered = asyncio.Event()

    class WaitingSource:
        """취소될 때까지 외부 요청을 기다리는 원천이다."""

        async def collect(self, source: SourceMapping, observed_at: str) -> list[WorkItem]:
            """실제 대기 시간 없이 종료 취소를 재현한다."""
            entered.set()
            await asyncio.Event().wait()
            return []

    task = asyncio.create_task(
        synchronize(
            SOURCE, WaitingSource(), SQLiteSnapshots(db, scheduled=True, clock=clock), clock.now
        )
    )
    await asyncio.wait_for(entered.wait(), 2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert await db.fetch("SELECT lease_until FROM work_source_state") == [{"lease_until": None}]
    assert await db.fetch("SELECT status FROM sync_runs") == [{"status": "failed"}]
    assert await db.fetch("SELECT item_id FROM item_snapshots") == []


async def test_migration_preserves_existing_snapshots(db: Database) -> None:
    """기존 버전 DB를 명시적으로 올려도 정상 자료는 그대로 남는다."""
    from src.infrastructure.sqlite.schema import SCHEMA_VERSION, migrate

    manual = SQLiteSnapshots(db)
    run = await manual.begin_run(SOURCE, OBSERVED)
    await manual.complete_run(run, [normalize_page(page(), SOURCE, OBSERVED)], OBSERVED)
    query = "SELECT item_id,payload,semantic_hash,mapping_hash,availability FROM item_snapshots"
    published = await db.fetch(query)
    assert len(published) == 1 and published[0]["item_id"] == "fake-page"
    with db.transaction() as connection:
        connection.execute("DROP TABLE sync_schedules")
        connection.execute("DELETE FROM schema_versions WHERE version=2")
    await migrate(db)
    assert await db.fetch(query) == published
    assert await db.fetch("SELECT source_id FROM sync_schedules") == []
    assert await db.fetch("SELECT version FROM schema_versions ORDER BY version") == [
        {"version": version} for version in range(1, SCHEMA_VERSION + 1)
    ]
