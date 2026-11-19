"""알림 예약·정정·중복과 전달권을 임시 SQLite에서 검증한다."""

from dataclasses import replace
from datetime import UTC, datetime, time

from src.infrastructure.sqlite.connection import Database
from tests.fixtures.clock import Clock


async def test_departure_schedule_reschedule_and_once_per_offset(db: Database) -> None:
    """출근 정정은 대기 예약만 바꾸고 이미 요청한30분 알림은 다시 만들지 않는다."""
    from src.domain.daily_work import DayRecord, FlexibleDeparturePolicy, LeaveType, WorkRules
    from src.domain.work_dates import SEOUL
    from src.infrastructure.sqlite.day_records import SQLiteDayStore
    from src.infrastructure.sqlite.notifications import SQLiteNotifications

    clock = Clock(datetime(2026, 11, 16, 8, tzinfo=SEOUL).timestamp())
    rules = WorkRules(480, 240, 120, time(12), time(13), time(0), time(23, 59))
    store = SQLiteDayStore(db, now=clock.now, policy=FlexibleDeparturePolicy())
    first = await store.save(
        DayRecord(
            clock.now().astimezone(SEOUL).date(),
            attendance=clock.now(),
            leave=LeaveType.NONE,
            rules=rules,
        ),
        0,
    )
    queue = SQLiteNotifications(db, now=clock.now)
    jobs = await db.fetch("SELECT due_at FROM notification_jobs ORDER BY due_at")
    # 기존 구독과 호환되는 다섯 시각이 실제로 저장되어야 한다.
    departure = datetime(2026, 11, 16, 17, tzinfo=SEOUL).timestamp()
    assert [row["due_at"] for row in jobs] == [
        departure - minutes * 60 for minutes in (30, 10, 5, 3, 1)
    ]
    clock.value = datetime(2026, 11, 16, 16, 30, tzinfo=SEOUL).timestamp()
    claimed = await queue.claim()
    assert len(claimed) == 1
    assert "30분" in claimed[0].title
    assert await queue.authorize(claimed[0].id, claimed[0].token)
    await queue.ack(claimed[0].id, claimed[0].token, "requested")
    changed = await store.save(
        replace(first, attendance=datetime(2026, 11, 16, 9, tzinfo=SEOUL)), 1
    )
    assert await db.fetch(
        "SELECT COUNT(*) AS n FROM notification_jobs WHERE state='requested'"
    ) == [{"n": 1}]
    clock.value = datetime(2026, 11, 16, 17, 30, tzinfo=SEOUL).timestamp()
    assert await queue.claim() == ()
    clock.value = datetime(2026, 11, 16, 17, 50, tzinfo=SEOUL).timestamp()
    second = await queue.claim()
    assert len(second) == 1 and "10분" in second[0].title
    await store.save(replace(changed, leave=LeaveType.FULL), 2)
    assert not await queue.authorize(second[0].id, second[0].token)


async def test_claim_is_unique_and_abandoned_delivery_becomes_unknown(db: Database) -> None:
    """중복 실행을 막고 요청 결과가 불명확하면 자동 재전송하지 않는다."""
    import asyncio

    from src.infrastructure.sqlite.notifications import SQLiteNotifications

    clock = Clock()
    with db.transaction() as connection:
        connection.execute(
            "INSERT INTO notification_jobs(id,logical_key,kind,title,body,due_at,expires_at,state) VALUES('fake','fake','calendar','새 일정','가상 일정',?,?, 'pending')",
            (clock.value, clock.value + 86400),
        )
    queue = SQLiteNotifications(db, now=clock.now)
    results = await asyncio.gather(queue.claim(), queue.claim())
    assert sum(len(items) for items in results) == 1
    clock.value += 31
    assert await queue.claim() == ()
    assert await db.fetch("SELECT state FROM notification_jobs") == [{"state": "unknown"}]


async def test_new_calendar_is_personal_and_initial_snapshot_is_only_baseline(db: Database) -> None:
    """최초·타인·취소·재등장은 제외하고 새 본인 일정만 한 번 예약한다."""
    from src.domain.models import CalendarEvent, Period, SourceMapping
    from src.infrastructure.sqlite.snapshots import SQLiteSnapshots

    clock = Clock()
    observed = clock.now().isoformat()
    source = SourceMapping(
        "meeting", "fake-calendar", {"title": "t", "participants": "p", "period": "d"}
    )
    store = SQLiteSnapshots(db, clock=clock, notification_user_id="fake-self")
    period = Period(
        "instant",
        datetime.fromtimestamp(clock.value + 3600, UTC).isoformat(),
        datetime.fromtimestamp(clock.value + 7200, UTC).isoformat(),
    )
    initial = CalendarEvent(
        "initial",
        source.source_id,
        "https://www.notion.so/fake",
        observed,
        observed,
        "기존 회의",
        ("fake-self",),
        period,
    )
    run = await store.begin_run(source, observed)
    await store.complete_run(run, [initial], observed)
    assert await db.fetch("SELECT id FROM notification_jobs") == []
    new = replace(initial, id="new", title="새 회의")
    other = replace(initial, id="other", participant_ids=("fake-other",))
    cancelled = replace(initial, id="cancelled", cancelled=True)
    run = await store.begin_run(source, observed)
    await store.complete_run(run, [new, other, cancelled], observed)
    assert await db.fetch("SELECT body FROM notification_jobs") == [{"body": "새 회의"}]
    run = await store.begin_run(source, observed)
    await store.complete_run(run, [initial, new], observed)
    assert len(await db.fetch("SELECT id FROM notification_jobs")) == 1


async def test_correction_after_claim_cancels_old_and_preserves_new_reservation(
    db: Database,
) -> None:
    """claim된 옛 예약을 승인하지 않고 새 버전의 알림을 잃지 않는다."""
    from src.domain.daily_work import DayRecord, FlexibleDeparturePolicy, LeaveType, WorkRules
    from src.domain.work_dates import SEOUL
    from src.infrastructure.sqlite.day_records import SQLiteDayStore
    from src.infrastructure.sqlite.notifications import SQLiteNotifications

    clock = Clock(datetime(2026, 11, 16, 8, tzinfo=SEOUL).timestamp())
    rules = WorkRules(480, 240, 120, time(12), time(13), time(0), time(23, 59))
    store = SQLiteDayStore(db, now=clock.now, policy=FlexibleDeparturePolicy())
    first = await store.save(
        DayRecord(
            clock.now().astimezone(SEOUL).date(),
            attendance=clock.now(),
            leave=LeaveType.NONE,
            rules=rules,
        ),
        0,
    )
    clock.value = datetime(2026, 11, 16, 16, 30, tzinfo=SEOUL).timestamp()
    queue = SQLiteNotifications(db, now=clock.now)
    old = (await queue.claim())[0]
    await store.save(replace(first, attendance=datetime(2026, 11, 16, 9, tzinfo=SEOUL)), 1)
    assert not await queue.authorize(old.id, old.token)
    await queue.ack(old.id, old.token, "cancelled")
    clock.value = datetime(2026, 11, 16, 17, 30, tzinfo=SEOUL).timestamp()
    fresh = await queue.claim()
    assert len(fresh) == 1 and fresh[0].id != old.id
    assert await queue.authorize(fresh[0].id, fresh[0].token)


async def test_calendar_cancelled_before_delivery_is_not_requested(db: Database) -> None:
    """새 일정이라도 이후 취소가 확인되면 대기 알림을 보내지 않는다."""
    from src.domain.models import CalendarEvent, Period, SourceMapping
    from src.infrastructure.sqlite.notifications import SQLiteNotifications
    from src.infrastructure.sqlite.snapshots import SQLiteSnapshots

    clock = Clock()
    observed = clock.now().isoformat()
    source = SourceMapping(
        "meeting", "fake-calendar", {"title": "t", "participants": "p", "period": "d"}
    )
    store = SQLiteSnapshots(db, clock=clock, notification_user_id="fake-self")
    first = await store.begin_run(source, observed)
    await store.complete_run(first, [], observed)
    period = Period("instant", datetime.fromtimestamp(clock.value + 3600, UTC).isoformat(), None)
    item = CalendarEvent(
        "new",
        source.source_id,
        "https://www.notion.so/fake",
        observed,
        observed,
        "회의",
        ("fake-self",),
        period,
    )
    second = await store.begin_run(source, observed)
    await store.complete_run(second, [item], observed)
    third = await store.begin_run(source, observed)
    await store.complete_run(third, [replace(item, cancelled=True)], observed)
    assert await SQLiteNotifications(db, now=clock.now).claim() == ()


async def test_queue_only_claims_current_source_and_user(db: Database) -> None:
    """설정에서 제거한 소스나 이전 사용자 알림을 전달하지 않는다."""
    from src.infrastructure.sqlite.notifications import SQLiteNotifications, notification_key

    clock = Clock()
    current = notification_key("fake-source:fake-self")
    other = notification_key("fake-source:fake-other")
    with db.transaction() as connection:
        for identifier, scope in (("current", current), ("other", other)):
            connection.execute(
                "INSERT INTO notification_jobs(id,logical_key,kind,title,body,due_at,expires_at,state,source_key) VALUES(?,?,'calendar','새 일정','테스트',?,?,'pending',?)",
                (identifier, identifier, clock.value, clock.value + 3600, scope),
            )
    queue = SQLiteNotifications(db, now=clock.now, source_keys=(current,))
    jobs = await queue.claim()
    assert len(jobs) == 1
    # 실행권 ID로 연결된 논리 예약이 현재 소스의 것인지 검사한다.
    assert await db.fetch(
        "SELECT job_id FROM notification_deliveries WHERE id=?", (jobs[0].id,)
    ) == [{"job_id": "current"}]
    assert await queue.authorize(jobs[0].id, jobs[0].token)
    assert await queue.claim() == ()


async def test_calendar_point_in_past_is_not_a_new_upcoming_event(db: Database) -> None:
    """종료 없는 시점 일정도 시작이 지나면 새 알림으로 만들지 않는다."""
    from src.domain.models import CalendarEvent, Period, SourceMapping
    from src.infrastructure.sqlite.notifications import SQLiteNotifications
    from src.infrastructure.sqlite.snapshots import SQLiteSnapshots

    clock = Clock()
    observed = clock.now().isoformat()
    source = SourceMapping(
        "meeting", "fake-point", {"title": "t", "participants": "p", "period": "d"}
    )
    store = SQLiteSnapshots(db, clock=clock, notification_user_id="fake-self")
    run = await store.begin_run(source, observed)
    await store.complete_run(run, [], observed)
    point = Period("instant", datetime.fromtimestamp(clock.value - 86400, UTC).isoformat(), None)
    item = CalendarEvent(
        "point",
        source.source_id,
        "https://www.notion.so/fake",
        observed,
        observed,
        "지난 일정",
        ("fake-self",),
        point,
    )
    run = await store.begin_run(source, observed)
    await store.complete_run(run, [item], observed)
    assert await SQLiteNotifications(db, now=clock.now).claim() == ()


async def test_date_event_notification_expires_at_final_seoul_midnight(db: Database) -> None:
    """종일 일정의 마지막 날짜가 지난 뒤에는 새 일정 배너를 요청하지 않는다."""
    from src.domain.models import CalendarEvent, Period, SourceMapping
    from src.domain.work_dates import SEOUL
    from src.infrastructure.sqlite.notifications import SQLiteNotifications
    from src.infrastructure.sqlite.snapshots import SQLiteSnapshots

    clock = Clock(datetime(2026, 11, 16, 23, tzinfo=SEOUL).timestamp())
    observed = clock.now().isoformat()
    source = SourceMapping(
        "meeting", "fake-date", {"title": "t", "participants": "p", "period": "d"}
    )
    store = SQLiteSnapshots(db, clock=clock, notification_user_id="fake-self")
    run = await store.begin_run(source, observed)
    await store.complete_run(run, [], observed)
    item = CalendarEvent(
        "day",
        source.source_id,
        "https://www.notion.so/fake",
        observed,
        observed,
        "종일 일정",
        ("fake-self",),
        Period("date", "2026-11-16", None),
    )
    run = await store.begin_run(source, observed)
    await store.complete_run(run, [item], observed)
    clock.value = datetime(2026, 11, 17, 0, tzinfo=SEOUL).timestamp()
    assert await SQLiteNotifications(db, now=clock.now).claim() == ()
