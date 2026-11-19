"""일정 필드의 왕복 저장·기존 캐시·해시 호환을 검증한다."""

import json
from dataclasses import replace
from datetime import UTC, datetime

import pytest

from src.application.errors import QueryReadError
from src.application.today_models import TodayContext
from src.application.use_cases.get_my_today import get_my_today
from src.domain.models import CalendarEvent, Period, SourceMapping
from src.domain.policies import ParticipationPolicy
from src.infrastructure.sqlite.connection import Database
from src.infrastructure.sqlite.serialization import fingerprint, source_fingerprint
from src.infrastructure.sqlite.snapshots import SQLiteSnapshots
from src.infrastructure.sqlite.work_reader import SQLiteWorkReader

STAMP = "2026-11-16T00:00:00Z"


def source(mapped: bool = True) -> SourceMapping:
    """필드 매핑 변경만 비교할 수 있는 가상 소스를 만든다."""
    properties = {"title": "t", "participants": "p", "period": "d"}
    if mapped:
        properties.update({"category": "c", "cancelled": "x"})
    return SourceMapping(
        "meeting", "fake-calendar", properties, event_types={"회의": "meeting"} if mapped else {}
    )


def event() -> CalendarEvent:
    """실제 자료와 무관한 회의 fixture다."""
    return CalendarEvent(
        "fake-event",
        "fake-calendar",
        "https://notion.so/fake",
        STAMP,
        STAMP,
        "가상 회의",
        ("me",),
        Period("date", "2026-11-16", None),
        category="회의",
    )


def test_legacy_source_hash_is_unchanged() -> None:
    """비어 있는 새 설정 필드가 기존 소스의 캐시를 무효화하지 않는다."""
    mapping = source(False)
    old = {
        "kind": "meeting",
        "source_id": "fake-calendar",
        "properties": {"title": "t", "participants": "p", "period": "d"},
        "approved_values": (),
    }
    assert source_fingerprint(mapping) == fingerprint(old)
    assert source_fingerprint(source()) != source_fingerprint(mapping)


async def test_cancel_and_restore_are_persisted_but_filtered(db: Database) -> None:
    """취소 근거는 남기고 현재 조회는 제외하며 취소 해제도 반영한다."""
    mapping = source()
    store = SQLiteSnapshots(db)
    reader = SQLiteWorkReader(db)
    for cancelled in (False, True, False):
        run = await store.begin_run(mapping, STAMP)
        await store.complete_run(run, [replace(event(), cancelled=cancelled)], STAMP)
        snapshots = await reader.read_sources((mapping,))
        assert snapshots[0].items[0].cancelled is cancelled
        result = await get_my_today(
            "2026-11-16",
            reader,
            TodayContext("me", (mapping,)),
            ParticipationPolicy(),
            lambda: datetime(2026, 11, 16, tzinfo=UTC),
        )
        assert len(result["meetings"]) == (0 if cancelled else 1)


@pytest.mark.parametrize(
    "removed",
    [
        ("category",),
        ("event_type",),
        ("cancelled",),
        ("category", "event_type"),
        ("category", "cancelled"),
        ("event_type", "cancelled"),
        ("category", "event_type", "cancelled"),
    ],
)
async def test_legacy_payload_and_new_payload_missing_keys(
    db: Database, removed: tuple[str, ...]
) -> None:
    """기존 payload만 기본값을 허용하고 새 필드의 부분 누락은 실패시킨다."""
    for mapping in (source(False), source()):
        store = SQLiteSnapshots(db)
        run = await store.begin_run(mapping, STAMP)
        await store.complete_run(run, [event()], STAMP)
        row = (await db.fetch("SELECT payload FROM item_snapshots"))[0]
        payload = json.loads(row["payload"])
        for key in removed:
            payload.pop(key)
        with db.transaction() as connection:
            connection.execute("UPDATE item_snapshots SET payload=?", (json.dumps(payload),))
        if mapping.event_types or len(removed) != 3:
            with pytest.raises(QueryReadError):
                await SQLiteWorkReader(db).read_sources((mapping,))
        else:
            snapshots = await SQLiteWorkReader(db).read_sources((mapping,))
            assert snapshots[0].items[0].event_type == "meeting"


async def test_calendar_navigation_reuses_projection_and_refreshes_current_data(
    db: Database,
) -> None:
    """월 이동은 같은 투영을 쓰고, 시각이 같은 재수집과 별도 근무 정정도 반영한다."""
    from datetime import date

    from src.application.calendar import CalendarService, CalendarSource
    from src.domain.daily_work import DayRecord
    from src.infrastructure.sqlite.day_records import SQLiteDayStore

    mapping = source()
    store = SQLiteSnapshots(db)
    now = datetime.fromisoformat(STAMP)
    days = SQLiteDayStore(db, now=lambda: now)
    calendar = CalendarService(
        SQLiteWorkReader(db),
        days,
        (CalendarSource(mapping, True, None, None),),
        ParticipationPolicy(),
        user_id="me",
        max_age_seconds=1200,
        now=lambda: now,
    )
    run = await store.begin_run(mapping, STAMP)
    await store.complete_run(run, [event()], STAMP)
    november = await calendar.month("2026-11")
    assert len(november.items) == 1
    assert not (await calendar.month("2026-12")).items
    await days.save(DayRecord(now.date(), attendance=now), 0)
    again = await calendar.month("2026-11")
    assert again.items[0] is november.items[0]
    assert again.attendance[0].day == now.date()

    run = await store.begin_run(mapping, STAMP)
    await store.complete_run(
        run, [replace(event(), period=Period("date", "2026-12-20", None))], STAMP
    )
    assert not (await calendar.month("2026-11")).items
    assert (await calendar.month("2026-12")).items[0].first == date(2026, 12, 20)
    run = await store.begin_run(mapping, STAMP)
    await store.fail_run(run, "source_failed")
    failed = await calendar.month("2026-12")
    assert failed.stale and len(failed.items) == 1
