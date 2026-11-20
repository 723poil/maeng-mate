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


async def test_history_compares_fiftieth_change_and_preserves_current_rules(db: Database) -> None:
    """표시 한도 바로 밖의 이력과 비교하고 확정에는 현재 기록의 메모와 규칙을 유지한다."""
    from src.application.work_records import WorkRecordsService
    from src.domain.daily_work import DEFAULT_WORK_RULES, DayRecord, FlexibleDeparturePolicy
    from src.infrastructure.sqlite.day_records import SQLiteDayStore

    clock = Clock(datetime(2026, 11, 16, 10, tzinfo=UTC).timestamp())
    day = clock.now().date()
    store = SQLiteDayStore(db, now=clock.now)
    service = WorkRecordsService(store, now=clock.now, policy=FlexibleDeparturePolicy())
    record = DayRecord(day, plan="가상 계획", rules=DEFAULT_WORK_RULES)
    for minute in range(51):
        record = await store.correct(
            replace(record, attendance=clock.now().replace(hour=0, minute=minute)), record.revision
        )

    detail = await service.detail(day)
    assert detail.truncated and len(detail.history) == 50
    assert detail.history[-1].revision == 2
    assert detail.history[-1].changes == ("출근 09:00 → 09:01",)
    assert detail.item.arrival == "09:50"

    confirmed = await service.confirm(day, record.revision)
    assert confirmed.item.departure == "17:50"
    assert confirmed.history[0].changes == ("퇴근 미기록 → 17:50",)
    current = await store.read(day)
    assert current.rules == record.rules and current.plan == record.plan


@pytest.mark.parametrize(
    ("arrival", "with_break", "expected_clock", "overnight"),
    [
        ("15:59", False, "23:59", False),
        ("16:00", False, "00:00", True),
        ("16:01", False, "00:01", True),
        ("15:59", True, "00:01", True),
    ],
)
async def test_overnight_preview_detail_confirmation_and_queue(
    db: Database, arrival: str, with_break: bool, expected_clock: str, overnight: bool
) -> None:
    """임시 기록에서 자정 경계의 미리보기·저장·상세·확정·예약을 함께 확인한다."""
    from datetime import timedelta

    from src.application.day_ports import DayInputError
    from src.application.day_records import DayService
    from src.application.desktop import DayUpdate, preview_departure
    from src.application.work_records import WorkRecordsService
    from src.domain.daily_work import (
        DEFAULT_WORK_RULES,
        OVERNIGHT_DEPARTURE_REASON,
        BreakWindow,
        FlexibleDeparturePolicy,
        LeaveType,
    )
    from src.domain.work_dates import SEOUL
    from src.infrastructure.sqlite.day_records import SQLiteDayStore
    from tests.fixtures.preferences import enable_notifications

    policy = FlexibleDeparturePolicy()
    clock = Clock(datetime(2026, 11, 16, 8, tzinfo=SEOUL).timestamp())
    rules = replace(
        DEFAULT_WORK_RULES,
        extra_breaks=(BreakWindow(time(20), time(20, 2)),) if with_break else (),
    )
    enable_notifications(db, "departure_30", "departure_0")
    store = SQLiteDayStore(db, now=clock.now, policy=policy, current_rules=lambda: rules)
    day_service = DayService(store, policy, now=clock.now)
    records = WorkRecordsService(store, now=clock.now, policy=policy)
    day = clock.now().astimezone(SEOUL).date()
    update = DayUpdate(day.isoformat(), 0, arrival, LeaveType.NONE, "가상 계획")
    preview = preview_departure(update, rules, policy=policy)
    assert preview.state == ("overnight" if overnight else "ready")
    assert preview.departure is not None
    assert preview.departure.strftime("%H:%M") == expected_clock
    assert await db.fetch("SELECT day FROM day_records") == []
    saved = await day_service.update(update.payload())
    assert saved["attendanceTime"] == arrival
    assert saved["calculationState"] == preview.state
    assert saved["expectedDepartureAt"] == preview.departure.astimezone(UTC).isoformat()
    detail = await records.detail(day)
    assert detail.expected_departure == preview.departure
    assert detail.payload()["confirmationBlockedReason"] == (
        OVERNIGHT_DEPARTURE_REASON if overnight else None
    )
    assert not detail.can_confirm
    jobs = await db.fetch("SELECT id FROM notification_jobs WHERE state='pending'")
    assert len(jobs) == (0 if overnight else 2)
    clock.value = (preview.departure + timedelta(minutes=1)).timestamp()
    detail = await records.detail(day)
    assert detail.can_confirm is not overnight
    if overnight:
        with pytest.raises(DayInputError):
            await records.confirm(day, detail.item.revision)
        assert (await store.read(day)).checkout_at is None
        assert len(await db.fetch("SELECT revision FROM day_record_revisions")) == 1
    else:
        confirmed = await records.confirm(day, detail.item.revision)
        assert confirmed.item.departure == "23:59"
