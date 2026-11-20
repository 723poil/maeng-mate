"""알림 예약·정정·중복과 전달권을 임시 SQLite에서 검증한다."""

from dataclasses import replace
from datetime import UTC, datetime, time

import pytest

from src.infrastructure.sqlite.connection import Database
from tests.fixtures.clock import Clock
from tests.fixtures.preferences import enable_notifications


async def test_departure_schedule_reschedule_and_once_per_offset(db: Database) -> None:
    """출근 정정은 대기 예약만 바꾸고 이미 요청한30분 알림은 다시 만들지 않는다."""
    from src.domain.daily_work import DayRecord, FlexibleDeparturePolicy, LeaveType, WorkRules
    from src.domain.work_dates import SEOUL
    from src.infrastructure.sqlite.day_records import SQLiteDayStore
    from src.infrastructure.sqlite.notifications import SQLiteNotifications

    enable_notifications(db, "departure_30", "departure_10", "departure_7")
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
    # 직접 지정한 세 시각만 실제로 저장되어야 한다.
    departure = datetime(2026, 11, 16, 17, tzinfo=SEOUL).timestamp()
    assert [row["due_at"] for row in jobs] == [departure - minutes * 60 for minutes in (30, 10, 7)]
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

    enable_notifications(db, "calendar")
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
        "meeting",
        "fake-calendar",
        {"title": "t", "participants": "p", "period": "d"},
        event_alert_triggers=frozenset({"calendar"}),
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

    enable_notifications(db, "departure_30")
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

    enable_notifications(db, "calendar")
    clock = Clock()
    observed = clock.now().isoformat()
    source = SourceMapping(
        "meeting",
        "fake-calendar",
        {"title": "t", "participants": "p", "period": "d"},
        event_alert_triggers=frozenset({"calendar"}),
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

    enable_notifications(db, "calendar")
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

    enable_notifications(db, "calendar")
    clock = Clock()
    observed = clock.now().isoformat()
    source = SourceMapping(
        "meeting",
        "fake-point",
        {"title": "t", "participants": "p", "period": "d"},
        event_alert_triggers=frozenset({"calendar"}),
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

    enable_notifications(db, "calendar")
    clock = Clock(datetime(2026, 11, 16, 23, tzinfo=SEOUL).timestamp())
    observed = clock.now().isoformat()
    source = SourceMapping(
        "meeting",
        "fake-date",
        {"title": "t", "participants": "p", "period": "d"},
        event_alert_triggers=frozenset({"calendar"}),
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


async def test_field_alerts_keep_missing_baseline_and_retire_pending_jobs(db: Database) -> None:
    """일시 누락·규칙 해제·담당 해제에서 기준과 미전달 예약의 수명을 구분한다."""
    from src.domain.models import SourceMapping, Task
    from src.domain.source_alerts import SourceAlertRule, alert_value_key
    from src.infrastructure.sqlite.snapshots import SQLiteSnapshots
    from tests.fixtures.notion import OBSERVED, SOURCE

    rule = SourceAlertRule("fixture-rule", "상태 변경", "s", "status")
    source = replace(SOURCE, alert_rules=(rule,))
    other = replace(source, source_id="other-source")
    store = SQLiteSnapshots(db, clock=Clock(), notification_user_id="me")
    item = Task(
        "same-id",
        source.source_id,
        "https://example.com",
        OBSERVED,
        OBSERVED,
        "가상 업무",
        ("me",),
        "진행",
        None,
        alert_values={rule.id: alert_value_key("진행")},
    )
    changed = replace(item, alert_values={rule.id: alert_value_key("검토")})

    async def publish(mapping: SourceMapping, items: list[Task]) -> None:
        """실제 확정 경로로 한 번의 가상 소스 수집을 저장한다."""
        await store.complete_run(await store.begin_run(mapping, OBSERVED), items, OBSERVED)

    for mapping in (source, other):
        await publish(mapping, [replace(item, source_id=mapping.source_id)])
        await publish(mapping, [replace(changed, source_id=mapping.source_id)])
    await publish(source, [])
    query = """SELECT job.state FROM notification_jobs job JOIN source_alert_events event
               ON event.job_id=job.id WHERE event.source_id=? ORDER BY job.id"""
    assert await db.fetch(query, (source.source_id,)) == [{"state": "obsolete"}]
    assert await db.fetch(query, (other.source_id,)) == [{"state": "pending"}]

    # 재등장할 때 값이 바뀌면 이전 값을 기준으로 새 알림이 정확히 한 번 생겨야 한다.
    await publish(source, [item])
    await publish(source, [item])
    assert sorted(row["state"] for row in await db.fetch(query, (source.source_id,))) == [
        "obsolete",
        "pending",
    ]
    await publish(source, [replace(item, assignee_ids=())])
    await publish(replace(other, alert_rules=()), [])
    assert await db.fetch("SELECT item_id FROM source_alert_baselines") == []
    assert await db.fetch("SELECT DISTINCT state FROM notification_jobs") == [{"state": "obsolete"}]


async def test_meeting_reschedule_invalidates_claim_and_respects_completed_delivery(
    db: Database,
) -> None:
    """정정된 회의는 이전 실행권을 막고 새 알림이 접수된 뒤에는 다시 예약하지 않는다."""
    from datetime import timedelta

    from src.domain.meeting_reminders import TenMinuteMeetingReminderPolicy
    from src.domain.models import CalendarEvent, Period, SourceMapping
    from src.infrastructure.sqlite.notifications import SQLiteNotifications
    from src.infrastructure.sqlite.snapshots import SQLiteSnapshots

    enable_notifications(db, "meeting")
    clock = Clock()
    observed = clock.now().isoformat()
    mapping = SourceMapping(
        "meeting",
        "fixture-meeting",
        {"title": "t", "participants": "p", "period": "d"},
        event_alert_triggers=frozenset({"meeting"}),
    )
    store = SQLiteSnapshots(
        db,
        clock=clock,
        notification_user_id="me",
        meeting_reminder_policy=TenMinuteMeetingReminderPolicy(),
    )
    queue = SQLiteNotifications(db, now=clock.now, user_id="me")
    meeting = CalendarEvent(
        "item",
        mapping.source_id,
        "https://www.notion.so/fixture",
        observed,
        observed,
        "회의",
        ("me",),
        Period("instant", (clock.now() + timedelta(minutes=5)).isoformat(), None),
    )

    async def publish(item: CalendarEvent) -> None:
        """같은 회의를 다시 수집하고 정상 스냅샷으로 확정한다."""
        await store.complete_run(await store.begin_run(mapping, observed), [item], observed)

    await publish(meeting)
    renamed = replace(meeting, title="수정한 제목", url="https://www.notion.so/revised")
    await publish(renamed)
    (old,) = await queue.claim()
    assert "수정한 제목" in old.body and old.url == renamed.url
    assert await queue.authorize(old.id, old.token)

    moved = replace(
        renamed, period=Period("instant", (clock.now() + timedelta(minutes=8)).isoformat(), None)
    )
    await publish(moved)
    assert not await queue.authorize(old.id, old.token)
    await queue.ack(old.id, old.token, "cancelled")
    (fresh,) = await queue.claim()
    assert fresh.id != old.id and await queue.authorize(fresh.id, fresh.token)
    await queue.ack(fresh.id, fresh.token, "requested")
    await publish(meeting)
    assert await queue.claim() == ()
    assert len(await db.fetch("SELECT id FROM notification_jobs WHERE kind='meeting'")) == 2


async def test_column_reminders_follow_source_dates_and_survive_restart(db: Database) -> None:
    """날짜·시각 정정과 누락이 영속 예약 및 이미 받은 실행권에 반영된다."""
    from uuid import uuid4

    from src.domain.source_alerts import AlertSchedule, SourceAlertRule
    from src.infrastructure.notion.normalization import normalize_page
    from src.infrastructure.sqlite.notifications import SQLiteNotifications
    from src.infrastructure.sqlite.snapshots import SQLiteSnapshots
    from tests.fixtures.notion import SOURCE, page

    rules = tuple(
        SourceAlertRule(str(uuid4()), name, "d", "date", schedule=schedule, time_only=time_only)
        for name, schedule, time_only in (
            ("전날", AlertSchedule("previous_day", "09:15"), False),
            ("당일", AlertSchedule("same_day", "10:00"), False),
            ("시작 전", AlertSchedule("before_start", "25"), True),
        )
    )
    source = replace(SOURCE, alert_rules=rules)
    enable_notifications(db)
    clock = Clock(datetime(2026, 12, 30, tzinfo=UTC).timestamp())
    snapshots = SQLiteSnapshots(db, clock=clock, notification_user_id="me")

    async def publish(start: str | None, *, missing: bool = False) -> None:
        """Notion 형식의 가상 날짜를 정규화해 실제 수집 트랜잭션을 완료한다."""
        stamp = clock.now().isoformat()
        raw = page()
        props = raw["properties"]
        assert isinstance(props, dict)
        props["Date"] = {
            "id": "d",
            "type": "date",
            "date": {"start": start, "end": None} if start else None,
        }
        items = [] if missing else [normalize_page(raw, source, stamp)]
        await snapshots.complete_run(await snapshots.begin_run(source, stamp), items, stamp)

    async def due_times() -> dict[str, datetime]:
        """중복 예약까지 검출하도록 대기 건수와 제목별 예정 시각을 함께 확인한다."""
        rows = await db.fetch("SELECT title,due_at FROM notification_jobs WHERE state='pending'")
        times = {r["title"]: datetime.fromtimestamp(r["due_at"], UTC) for r in rows}
        assert len(times) == len(rows)
        return times

    # UTC 12월 31일 저녁은 서울 1월 1일이다. 분 단위 규칙은 원본의 초도 유지한다.
    start = "2026-12-31T17:30:40Z"
    await publish(start)
    await publish(start)
    assert await due_times() == {
        "전날": datetime(2026, 12, 31, 0, 15, tzinfo=UTC),
        "당일": datetime(2027, 1, 1, 1, tzinfo=UTC),
        "시작 전": datetime(2026, 12, 31, 17, 5, 40, tzinfo=UTC),
    }
    # 종일 값으로 바꾸면 시작 시각을 상정하지 않고 전날·당일 예약만 남긴다.
    await publish("2027-01-01")
    assert set(await due_times()) == {"전날", "당일"}
    await publish(None, missing=True)
    assert await due_times() == {}
    await publish(start)
    clock.value = datetime(2026, 12, 31, 0, 15, tzinfo=UTC).timestamp()
    queue = SQLiteNotifications(Database(str(db.path)), now=clock.now, user_id="me")
    (old,) = await queue.claim()
    assert old.title == "전날" and await queue.authorize(old.id, old.token)
    await publish("2027-01-02")
    assert not await queue.authorize(old.id, old.token)
    await queue.ack(old.id, old.token, "cancelled")
    await publish(None)
    assert await due_times() == {}
    # 이미 지난 예약을 뒤늦게 추가하지 않는다.
    await publish("2026-12-29")
    assert await due_times() == {} and await queue.claim() == ()


async def test_migrated_rules_compare_existing_rows_with_the_first_new_snapshot(
    db: Database,
) -> None:
    """기존 소스의 역할을 실제 칼럼에 연결하고 업데이트 전후의 변경만 한 번 발송한다."""
    from uuid import uuid4

    from src.application.work_settings import category_documents, empty_work_settings
    from src.bootstrap.environment import AppEnvironment
    from src.bootstrap.notification_wiring import build_notifications
    from src.domain.category_configuration import CategoryColumn, CategoryConfiguration
    from src.domain.category_event_alerts import CategoryEventAlert
    from src.domain.source_alerts import UNBOUND_COLUMN
    from src.domain.source_display import SourceCategory
    from src.infrastructure.notion.normalization import normalize_page
    from src.infrastructure.sqlite.snapshots import SQLiteSnapshots
    from src.infrastructure.sqlite.work_settings import SQLiteWorkSettings
    from tests.fixtures.notion import page

    person = CategoryColumn(str(uuid4()), "검토자", "text")
    status = CategoryColumn(str(uuid4()), "검토 단계", "text")
    config = CategoryConfiguration(
        columns=(person, status),
        event_alerts=(
            CategoryEventAlert("assigned", "새 검토자"),
            CategoryEventAlert("status", "검토 단계 변경"),
            CategoryEventAlert("due", "기한"),
        ),
    )
    category = SourceCategory(str(uuid4()), "검토", config)
    source = {
        "kind": "task",
        "dataSourceId": str(uuid4()),
        "categoryId": category.id,
        "properties": {"title": "t", "status": "s", "assignees": "p"},
        "columnMappings": {
            person.id: {"propertyId": "p", "kind": "people"},
            status.id: {"propertyId": "s", "kind": "status"},
        },
    }
    work = SQLiteWorkSettings(db)
    work.initialize(
        {
            **empty_work_settings(),
            "categoryAssignmentVersion": 1,
            "categories": category_documents((category,)),
            "sources": [source],
        }
    )
    enable_notifications(db, "task_assigned", "task_status", "task_due")
    stored = work.read_validated()
    assert stored is not None
    mapping = stored[1].active_sources[0]
    clock = Clock()
    stamp = clock.now().isoformat()
    snapshots = SQLiteSnapshots(db, clock=clock, notification_user_id="me")
    raw = page()
    initial = normalize_page(raw, mapping, stamp)
    await snapshots.complete_run(await snapshots.begin_run(mapping, stamp), [initial], stamp)
    converted = work.prepare_categories()
    assert converted is not None and work.prepare_categories() == converted
    settings = converted[1]
    saved = settings.categories[0].configuration
    assert saved is not None and saved.event_alerts == ()
    assigned, changed, unbound = (entry.rule for entry in saved.alerts)
    assert assigned.column_id == person.id and assigned.member == "self"
    assert changed.column_id == status.id and changed.enabled
    assert unbound.column_id == UNBOUND_COLUMN and not unbound.enabled
    # 이전 수집을 했던 기존 행은 상태 알림만, 새 행은 배정 알림만 발생한다.
    mapping = settings.active_sources[0]
    props = raw["properties"]
    assert isinstance(props, dict)
    props["Status"] = {"id": "s", "type": "status", "status": {"name": "검토 완료"}}
    items = [
        normalize_page(item, mapping, stamp, user_id="me") for item in (raw, {**raw, "id": "new"})
    ]
    await snapshots.complete_run(await snapshots.begin_run(mapping, stamp), items, stamp)
    await snapshots.complete_run(await snapshots.begin_run(mapping, stamp), items, stamp)
    queue, _ = build_notifications(
        AppEnvironment("fixture", database=str(db.path), notion_user_id="me"), settings
    )
    assert queue is not None
    records = await db.fetch("SELECT title FROM notification_jobs ORDER BY title")
    assert records == [{"title": name} for name in sorted([assigned.name, changed.name])]


async def test_overnight_rules_and_subscription_rescheduling_cancel_pending(
    db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    """당일에서 익일로 바뀐 규칙과 구독 재계산은 같은 예약 제한을 적용한다."""
    from src.domain.daily_work import DEFAULT_WORK_RULES, DayRecord, FlexibleDeparturePolicy
    from src.domain.work_dates import SEOUL
    from src.infrastructure.sqlite import day_records
    from src.infrastructure.sqlite.day_records import SQLiteDayStore, reschedule_today_departure
    from src.infrastructure.sqlite.preferences import SQLitePreferences

    clock = Clock(datetime(2026, 11, 16, 8, tzinfo=SEOUL).timestamp())
    policy = FlexibleDeparturePolicy()
    enable_notifications(db, "departure_30", "departure_0")
    store = SQLiteDayStore(db, now=clock.now, policy=policy)
    saved = await store.save(
        DayRecord(
            clock.now().astimezone(SEOUL).date(),
            attendance=datetime(2026, 11, 16, 15, 59, tzinfo=SEOUL),
            rules=DEFAULT_WORK_RULES,
        ),
        0,
    )
    assert len(await db.fetch("SELECT id FROM notification_jobs WHERE state='pending'")) == 2
    with store.apply_today_rules(replace(DEFAULT_WORK_RULES, daily_minutes=481)):
        pass
    assert await db.fetch("SELECT id FROM notification_jobs WHERE state='pending'") == []
    assert (await store.read(saved.day)).attendance == saved.attendance
    monkeypatch.setattr(day_records, "utc_now", clock.now)

    preferences = SQLitePreferences(db)
    preferences.save(replace(preferences.read(), notification_types=("departure_10",)))
    with db.transaction() as connection:
        reschedule_today_departure(connection, policy)
    assert await db.fetch("SELECT id FROM notification_jobs WHERE state='pending'") == []
    with store.apply_today_rules(DEFAULT_WORK_RULES):
        pass
    assert len(await db.fetch("SELECT id FROM notification_jobs WHERE state='pending'")) == 1
