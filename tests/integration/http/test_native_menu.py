"""macOS 네이티브 메뉴 객체와 실제 근무 저장 연결을 검증한다."""

import asyncio
import sys
import time
from datetime import UTC, datetime
from datetime import time as day_time
from pathlib import Path

import pytest


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS 전용 선택 UI")
def test_native_menu_renders_and_saves_attendance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """기본 팝오버의 시분 선택·즉시 계산·자동 저장이 서버 기록과 일치한다."""
    pytest.importorskip("AppKit")
    from src.application.day_records import DayService
    from src.application.desktop import (
        CollectionView,
        DayUpdate,
        DayView,
        DesktopArea,
        DesktopSnapshot,
        MenuEntry,
    )
    from src.domain.daily_work import FlexibleDeparturePolicy, WorkRules
    from src.domain.preferences import SectionPreference
    from src.domain.source_display import SourceCategory, SourceDisplay
    from src.infrastructure.day_response import day_view
    from src.infrastructure.sqlite.connection import Database
    from src.infrastructure.sqlite.day_records import SQLiteDayStore
    from src.infrastructure.sqlite.preferences import SQLitePreferences
    from src.infrastructure.sqlite.schema import migrate
    from src.presentation.menubar import MenuBarApp

    def now() -> datetime:
        """개인 정보 없는 날짜와 계산 시각을 고정한다."""
        return datetime(2026, 11, 16, 0, tzinfo=UTC)

    db = Database(str(tmp_path / "work.sqlite3"))
    asyncio.run(migrate(db))
    service = DayService(
        SQLiteDayStore(
            db,
            now=now,
            current_rules=lambda: WorkRules(
                480, 240, 120, day_time(12), day_time(13), day_time(0), day_time(23, 59)
            ),
        ),
        FlexibleDeparturePolicy(),
        now=now,
    )

    class Client:
        """UI의 네트워크만 대체하고 계산과 SQLite는 실제로 사용한다."""

        def fetch(self) -> DesktopSnapshot:
            """실제 저장된 근무 기록을 네이티브 표시 모델로 바꾼다."""
            tasks = tuple(
                MenuEntry(
                    f"테스트 업무 {index}",
                    "진행 중",
                    f"https://www.notion.so/task-{index}",
                    status="리뷰" if index == 0 else "홀드",
                    deadline="2026.11.20" if index == 0 else None,
                )
                for index in range(30)
            )
            meetings = (MenuEntry("테스트 회의", "14:00", "https://www.notion.so/meeting-0"),)
            return DesktopSnapshot(
                day_view(asyncio.run(service.read())),
                CollectionView(True, False, 2, 0, None, None),
                (),
                (),
                (),
                (0, 0, 0),
                True,
                ("completion_status_unconfigured",),
                category_areas={
                    tasks_card.key: DesktopArea(tasks, 30),
                    meetings_card.key: DesktopArea(meetings, 1),
                },
            )

        def save(self, update: DayUpdate) -> DayView:
            """서버 유스케이스가 확정한 결과만 반환한다."""
            return day_view(asyncio.run(service.update(update.payload())))

    from functools import partial

    from src.application.desktop import preview_departure

    tasks_card, meetings_card = (
        SourceCategory("fixture-tasks", "업무"),
        SourceCategory("fixture-meetings", "회의"),
    )
    display = (
        SourceDisplay(
            "tasks-db", "task", "tasks", url="https://www.notion.so/fake-tasks", category=tasks_card
        ),
        SourceDisplay(
            "meeting-db",
            "meeting",
            "tasks",
            url="https://www.notion.so/fake-meetings",
            category=meetings_card,
        ),
    )
    opened: list[str] = []

    from src.application.calendar import calendar_span
    from src.domain.calendar import CalendarMonth

    class MonthClient:
        """개인 원천 없이 메인 창의 실제 달력 조회와 선택을 확인한다."""

        def month(self, month: str | None = None) -> CalendarMonth:
            """요청한 월의 빈 격자를 만든다."""
            selected = month or "2026-11"
            first, last = calendar_span(selected)
            return CalendarMonth(
                selected, now().date(), first, last, (), (), False, False, False, now()
            )

    app = MenuBarApp(
        Client(),
        partial(preview_departure, policy=FlexibleDeparturePolicy()),
        preferences_client=SQLitePreferences(db),
        calendar_client=MonthClient(),
        open_url=opened.append,
        today=lambda: now().date().isoformat(),
    )

    def settle() -> None:
        """실제 긴 타이머를 기다리지 않고 메인 루프의 결과 반영을 실행한다."""
        deadline = time.monotonic() + 3
        while (
            app._controller.busy or app._preferences_controller.busy or app._dirty
        ) and time.monotonic() < deadline:
            app.tick()
            time.sleep(0.002)
        assert not app._controller.busy

    try:
        settle()
        assert app._collection.stringValue() == "수집 대기"
        # 첫 조회가 끝나도 메뉴바에는 아이콘만 남고 보조 설명에 상태가 반영된다.
        status = app._status_item.button()
        assert status.title() == ""
        assert status.image().isTemplate()
        assert status.toolTip() == status.accessibilityLabel() == "Maeng Mate · 수집 대기"
        assert app._view.frame().size.width == 520
        app._apply_source_display(display)
        task_card = app._custom_sections[tasks_card.key]
        meeting_card = app._custom_sections[meetings_card.key]
        assert task_card.entries._scroll.frame().size.width == 466
        # Finder만 메인 창을 열며, 사이드바와 본문은 숨긴 카테고리를 제외한다.
        from dataclasses import replace

        original = app._preferences
        app._preferences = replace(
            original,
            sections=tuple(
                SectionPreference(item.key, item.key == meetings_card.key)
                for item in app._section_order
            ),
        )
        app._resize_lists()
        assert app._main_window is None
        app._callbacks.applicationShouldHandleReopen_hasVisibleWindows_(None, False)
        main = app._main_window
        assert main is not None and main.window.isVisible()
        assert [item.key for item in main.cards] == [meetings_card.key]
        app.open_calendar()
        assert app._main_window is main and main.calendar and main.page
        main.fold()
        assert main.folded and main.calendar
        main.fold()
        app.open_main()
        app._preferences = replace(
            original,
            sections=tuple(SectionPreference(item.key, False) for item in app._section_order),
        )
        app._resize_lists()
        app._render_home()
        assert main.cards == () and main.headers == []
        app.open_settings(screen=True)
        assert app._settings_window is not None
        app._settings_window._window.close()
        main.window.close()
        app._preferences = original
        app._resize_lists()
        app.open_source_board(0)
        app.open_source_board(1)
        assert opened == ["https://www.notion.so/fake-tasks", "https://www.notion.so/fake-meetings"]
        assert "완료 기준 없음" not in app._collection_detail.stringValue()
        assert "미기록" in app._message.stringValue()
        assert app._departure.stringValue() == "18:00"
        assert asyncio.run(service.read())["revision"] == 0
        scroll = task_card.entries._scroll
        document = scroll.documentView()
        from AppKit import NSBox, NSColor, NSTextField

        controls = [control for row in document.subviews() for control in row.subviews()]
        fields = [view for view in controls if isinstance(view, NSTextField)]
        review = next(view for view in fields if view.stringValue() == "리뷰")
        assert any(view.stringValue() == "홀드" for view in fields)
        badges = [view for view in controls if isinstance(view, NSBox)]
        assert badges[0].fillColor() == NSColor.systemPurpleColor().colorWithAlphaComponent_(0.14)
        assert badges[1].fillColor() == NSColor.systemOrangeColor().colorWithAlphaComponent_(0.14)
        deadline = next(view for view in fields if view.stringValue() == "2026.11.20")
        from AppKit import NSButton

        title = next(
            view
            for view in controls
            if isinstance(view, NSButton) and view.title() == "테스트 업무 0"
        )
        assert review.frame().origin.x < deadline.frame().origin.x < title.frame().origin.x
        assert abs(review.frame().origin.y - deadline.frame().origin.y) <= 2
        review_center = review.frame().origin.y + review.frame().size.height / 2
        title_center = title.frame().origin.y + title.frame().size.height / 2
        assert abs(review_center - title_center) <= 1
        assert document.frame().size.height > scroll.frame().size.height
        document.scrollPoint_((0, 0))
        origin = scroll.contentView().bounds().origin.y
        app._render()
        assert scroll.contentView().bounds().origin.y == origin
        title.performClick_(None)
        assert opened[-1] == "https://www.notion.so/task-0"
        # 짧은 목록은 문서가 뷰포트와 함께 늘어나도 버튼과 위쪽 여백을 유지한다.
        meeting_scroll = meeting_card.entries._scroll
        short_doc = meeting_scroll.documentView()
        short_button = next(
            v for row in short_doc.subviews() for v in row.subviews() if isinstance(v, NSButton)
        )
        margin = (
            short_doc.frame().size.height
            - short_button.convertRect_toView_(short_button.bounds(), short_doc).origin.y
        )
        before = meeting_scroll.frame().size.height
        app.expand_source(1)
        assert meeting_scroll.frame().size.height == pytest.approx(before + 144)
        assert meeting_scroll.documentView() == short_doc
        assert short_doc.frame().size.height - short_button.convertRect_toView_(
            short_button.bounds(), short_doc
        ).origin.y == pytest.approx(margin, abs=0.5)
        app.expand_source(1)
        # 앞 카드의 행 수와 링크가 달라져도 뒤 카드의 문서와 클릭 목적지는 유지한다.
        snapshot = app._controller.snapshot
        assert snapshot is not None
        # 한 행의 링크·배지가 바뀌어도 같은 카드의 다른 행은 기존 버튼을 계속 쓴다.
        existing = snapshot.category_areas[tasks_card.key].entries
        changed = replace(existing[1], status="완료", url="https://www.notion.so/changed")
        task_card.entries.update((existing[0], changed, *existing[2:]))
        assert scroll.documentView() == document
        assert title in document.subviews()[0].subviews()
        updated = next(v for v in document.subviews()[1].subviews() if isinstance(v, NSButton))
        updated.performClick_(None)
        assert opened[-1] == changed.url
        app._render_source_lists(
            replace(
                snapshot,
                category_areas={**snapshot.category_areas, tasks_card.key: DesktopArea((), 0)},
            )
        )
        assert meeting_scroll.documentView() == short_doc
        short_button.performClick_(None)
        assert opened[-1] == "https://www.notion.so/meeting-0"
        app._render()
        # 긴 목록이 뷰포트보다 큰 동안에는 상단 기준으로 읽던 위치를 유지한다.
        monkeypatch.setattr(app, "_available_height", lambda: 740)
        app._resize_lists()
        document = scroll.documentView()
        document.scrollPoint_((0, document.frame().size.height - scroll.frame().size.height - 50))
        app.expand_source(0)
        assert app._view.frame().size.height <= 740
        assert scroll.documentView() == document
        distance = (
            document.frame().size.height
            - scroll.frame().size.height
            - scroll.contentView().bounds().origin.y
        )
        assert distance == pytest.approx(50, abs=1)
        app.expand_source(0)
        # 화면 숨김을 되돌릴 때는 같은 카드를 쓰고, 연결 자체가 사라지면 제거한다.
        original_preferences = app._preferences
        app._preferences = replace(
            original_preferences,
            sections=(*original_preferences.sections, SectionPreference(meetings_card.key, False)),
        )
        app._resize_lists()
        assert meeting_card.view.isHidden() and meeting_card.view.superview() == app._body
        app._preferences = original_preferences
        app._resize_lists()
        assert not meeting_card.view.isHidden() and meeting_scroll.documentView() == short_doc
        app._apply_source_display(display[:1])
        assert (
            meetings_card.key not in app._custom_sections and meeting_card.view.superview() is None
        )
        app._apply_source_display(display)
        assert app._custom_sections[meetings_card.key].view.superview() == app._body
        assert app.hour.indexOfSelectedItem() == 9
        assert asyncio.run(service.read())["revision"] == 0
        app.leave.selectItemAtIndex_(3)
        app.hour.selectItemAtIndex_(9)
        app.minute.selectItemAtIndex_(1)
        app.mark_dirty()
        assert app._departure.stringValue() == "14:01"
        settle()
        assert app._departure.stringValue() == "14:01"
        assert app.hour.indexOfSelectedItem() == 9
        assert app.minute.indexOfSelectedItem() == 1
        assert app.leave.indexOfSelectedItem() == 3
        record = asyncio.run(service.read())
        assert record["revision"] == 1
        assert record["attendanceTime"] == "09:01"
        # 첫 저장 응답이 나중에 도착해도 사용자의 다음 선택을 되돌리면 안 된다.
        app.hour.selectItemAtIndex_(10)
        app.minute.selectItemAtIndex_(0)
        app.leave.selectItemAtIndex_(0)
        app.mark_dirty()
        app._last_edit -= 1
        app.tick()
        assert app._controller.busy
        app.hour.selectItemAtIndex_(8)
        app.minute.selectItemAtIndex_(30)
        app.leave.selectItemAtIndex_(3)
        app.mark_dirty()
        assert app._departure.stringValue() == "12:30"
        settle()
        final = asyncio.run(service.read())
        assert final["attendanceTime"] == "08:30"
        assert final["leaveType"] == "pm_half"
        assert final["revision"] == 3
        assert app.hour.indexOfSelectedItem() == 8
        assert app.minute.indexOfSelectedItem() == 30
        assert app._departure.stringValue() == "12:30"
        # 자정 경계는 저장 전 미리보기와 저장 응답 뒤 모두 같은 이유를 보여 준다.
        app.hour.selectItemAtIndex_(16)
        app.minute.selectItemAtIndex_(0)
        app.leave.selectItemAtIndex_(0)
        app.mark_dirty()
        assert app._departure.stringValue() == "11/17 00:00"
        assert app._departure_warning.stringValue() == "익일 확정 불가 · 알림 없음"
        assert (
            app._departure_warning.fittingSize().width <= app._departure_warning.frame().size.width
        )
        settle()
        assert app._departure_warning.stringValue() == "익일 확정 불가 · 알림 없음"
        assert asyncio.run(service.read())["calculationState"] == "overnight"
        app.hour.selectItemAtIndex_(15)
        app.minute.selectItemAtIndex_(59)
        app.mark_dirty()
        assert app._departure_warning.stringValue() == ""
        settle()
        app._notification_status = lambda: "알림 연결 확인 필요"
        app.tick()
        assert "알림 연결 확인 필요" in (
            app._work_message.stringValue() or app._message.stringValue()
        )
    finally:
        app.close()


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS 네이티브 필터")
def test_calendar_filters_restore_rejected_click_and_refresh_replaced_controls() -> None:
    """거부된 클릭과 새로 만든 행은 이전에 그린 선택값과 같아도 다시 반영한다."""
    pytest.importorskip("AppKit")
    from AppKit import NSApplication, NSView

    from src.domain.calendar import CalendarPreferences
    from src.presentation.calendar_controls import CalendarFilterChoice, CalendarFilters
    from src.presentation.native_views import frame

    NSApplication.sharedApplication()
    parent = NSView.alloc().initWithFrame_(frame(0, 0, 200, 300))
    requests: list[tuple[str, bool]] = []

    def changed(key: str, visible: bool) -> None:
        """변경 요청은 기록하되 거부된 경우처럼 확정값을 유지한다."""
        requests.append((key, visible))

    filters = CalendarFilters(parent, 0, 200, changed)
    choice = CalendarFilterChoice("category:fixture", "가상 일정", "meeting")
    preferences = CalendarPreferences(hidden_categories=(choice.key,))
    filters.update_choices((choice,))
    filters.render(preferences, True)
    control = filters.controls[choice.key]
    control.performClick_(None)
    assert requests == [(choice.key, True)]
    filters.render(preferences, True)
    assert control.state() == 0

    filters.render(preferences, False)
    filters.update_choices((CalendarFilterChoice(choice.key, "수정한 이름", "task"),))
    filters.render(preferences, False)
    replacement = filters.controls[choice.key]
    assert replacement is not control
    assert not replacement.isEnabled() and replacement.state() == 0
    assert replacement.accessibilityLabel() == "수정한 이름"
