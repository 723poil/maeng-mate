"""개인 근무 기록의 동시 수정·이력 원자성을 검증한다."""

import asyncio
from dataclasses import replace
from datetime import UTC, datetime, time

import pytest

from src.infrastructure.sqlite.connection import Database
from tests.fixtures.clock import Clock


async def test_day_revision_conflict_and_history_rollback(db: Database) -> None:
    """같은 버전의 병렬 저장은 한 번만 성공하며 이력 실패도 전체 롤백한다."""
    from src.application.day_ports import DayConflictError
    from src.domain.daily_work import DayRecord, LeaveType, WorkRules
    from src.infrastructure.sqlite.day_records import SQLiteDayStore

    clock = Clock(datetime(2026, 11, 16, 0, tzinfo=UTC).timestamp())
    store = SQLiteDayStore(db, now=clock.now)
    empty = await store.read(clock.now().date())
    assert empty.revision == 0
    record = DayRecord(
        empty.day,
        attendance=clock.now(),
        leave=LeaveType.NONE,
        plan="오늘 계획",
        rules=WorkRules(480, 240, 120, time(12), time(13), time(0), time(23, 59)),
        updated_at=clock.now(),
    )
    results = await asyncio.gather(
        store.save(record, 0), store.save(record, 0), return_exceptions=True
    )
    assert sum(isinstance(item, DayConflictError) for item in results) == 1
    saved = await store.read(record.day)
    assert saved.revision == 1
    assert saved.plan == "오늘 계획"
    assert len(await db.fetch("SELECT revision FROM day_record_revisions")) == 1
    with db.transaction() as connection:
        connection.execute(
            "CREATE TRIGGER fail_history BEFORE INSERT ON day_record_revisions BEGIN SELECT RAISE(ABORT,'fake'); END"
        )
    with pytest.raises(ValueError):
        await store.save(replace(saved, plan="저장되면 안 됨"), 1)
    assert (await store.read(record.day)).plan == "오늘 계획"
    assert (await store.read(record.day)).revision == 1


async def test_midnight_rejects_previous_day_edit(db: Database) -> None:
    """자정을 지난 수정은 이전 날짜도 새 날짜도 덮어쓰지 않는다."""
    from src.application.day_ports import DayConflictError
    from src.domain.daily_work import DayRecord
    from src.infrastructure.sqlite.day_records import SQLiteDayStore

    clock = Clock(datetime(2026, 11, 16, 14, 59, tzinfo=UTC).timestamp())
    store = SQLiteDayStore(db, now=clock.now)
    record = DayRecord(clock.now().date())
    clock.value += 120
    with pytest.raises(DayConflictError):
        await store.save(record, 0)
    assert await db.fetch("SELECT day FROM day_records") == []
