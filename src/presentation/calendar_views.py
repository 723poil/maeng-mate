"""메인의 작은 달력과 Google Calendar식 별도 월 창을 구성한다."""

from collections.abc import Callable

from AppKit import (
    NSApplication,
    NSLineBreakByTruncatingTail,
    NSNoBorder,
    NSScreen,
    NSScrollView,
    NSView,
)

from src.domain.calendar import CalendarPreferences
from src.presentation.calendar_agenda import CalendarAgenda
from src.presentation.calendar_controller import CalendarController
from src.presentation.calendar_controls import (
    CalendarFilterChoice,
    CalendarFilters,
    CalendarToolbar,
    calendar_filter_choices,
)
from src.presentation.calendar_filter_popover import CalendarFilterPopover
from src.presentation.calendar_grid import CalendarGrid
from src.presentation.calendar_style import attendance_hint, attendance_summary, item_summary
from src.presentation.glass_panel import auxiliary_window
from src.presentation.native_views import card, frame, glass_container, label

COMPACT_CALENDAR_HEIGHT = 324.0


class CompactCalendarView:
    """작은 월 격자·종류 선택·선택 날짜 두 줄 요약을 한 카드에 모은다."""

    def __init__(
        self,
        parent: NSView,
        width: float,
        controller: CalendarController,
        preferences: Callable[[], CalendarPreferences],
        toggle: Callable[[str, bool], None],
        open_window: Callable[[], None],
        *,
        choices: Callable[[], tuple[CalendarFilterChoice, ...]] = lambda: calendar_filter_choices(
            ()
        ),
    ) -> None:
        """메인 영역 이동에서도 컨트롤을 새로 만들지 않는다."""
        self.controller, self.preferences = controller, preferences
        self.view = NSView.alloc().initWithFrame_(frame(16, 0, width - 32, COMPACT_CALENDAR_HEIGHT))
        parent.addSubview_(self.view)
        content_width = width - 32
        card(self.view, 0, 0, content_width, COMPACT_CALENDAR_HEIGHT)
        self.filter_popover = CalendarFilterPopover(preferences, toggle, choices)
        self.filters = self.filter_popover.filters
        self.toolbar = CalendarToolbar(
            self.view,
            0,
            COMPACT_CALENDAR_HEIGHT - 36,
            content_width,
            controller,
            compact=True,
            open_window=open_window,
            open_filters=self.filter_popover.show,
        )
        self.grid = CalendarGrid(
            self.view, 10, 68, content_width - 20, 32, controller, compact=True
        )
        self._day = label(self.view, "", 14, 48, content_width - 28, size=11, bold=True)
        self._first = label(self.view, "", 14, 28, content_width - 28, size=11)
        self._second = label(self.view, "", 14, 10, content_width - 28, size=11, secondary=True)

        for text in (self._day, self._first, self._second):
            text.cell().setLineBreakMode_(NSLineBreakByTruncatingTail)

    def set_hidden(self, hidden: bool) -> None:
        """메인에서 숨겨도 독립 창과 표시 선택은 유지한다."""
        if hidden:
            self.filter_popover.hide()
        self.view.setHidden_(hidden)

    def resize(self, y: float) -> None:
        """본문 스크롤 안에서 카드 위치만 갱신한다."""
        if self.view.frame()[0][1] != y:
            self.filter_popover.hide()
        self.view.setFrame_(frame(16, y, self.view.frame()[1][0], COMPACT_CALENDAR_HEIGHT))

    def render(self, enabled: bool) -> None:
        """선택 종류와 날짜를 두 화면에서 즉시 일치시킨다."""
        preferences = self.preferences()
        self.toolbar.render()
        self.filter_popover.render(enabled)
        if self.toolbar.filter_button is not None:
            self.toolbar.filter_button.setEnabled_(enabled)
        self.grid.render(preferences)
        data, day = self.controller.data, self.controller.selected
        if data is None or day is None:
            self._day.setStringValue_(self.controller.message)
            self._first.setStringValue_("")
            self._first.setToolTip_(None)
            self._second.setStringValue_("")
            return
        contents = self.controller.contents_on(day, preferences)
        items, record = contents.items, contents.attendance
        self._day.setStringValue_(
            f"{day.month}월 {day.day}일 · 일정 {len(items)}개"
            + (" · " + self.controller.message if self.controller.message else "")
        )
        lines = [item_summary(item, day) for item in items[:2]]
        if lines:
            self._first.setStringValue_(lines[0])
            self._first.setToolTip_(lines[0])
        else:
            self._first.setStringValue_(
                attendance_summary(record) if record else "표시할 일정 없음"
            )
            self._first.setToolTip_(attendance_hint(record) if record else None)
        self._second.setStringValue_(lines[1] if len(lines) > 1 else "")


class CalendarWindow:
    """좌측 표시 항목·월 격자·오른쪽 날짜 상세를 동시에 보여준다."""

    def __init__(
        self,
        controller: CalendarController,
        preferences: Callable[[], CalendarPreferences],
        toggle: Callable[[str, bool], None],
        open_item: Callable[[str | None], None],
        *,
        choices: Callable[[], tuple[CalendarFilterChoice, ...]] = lambda: calendar_filter_choices(
            ()
        ),
    ) -> None:
        """메인 달력과 같은 상태·설정 콜백을 주입한다."""
        self.controller, self.preferences = controller, preferences
        self._choices = choices
        screen = NSScreen.mainScreen()
        height = min(700.0, max(620.0, screen.visibleFrame()[1][1] - 80)) if screen else 680.0
        width = 1080.0
        self._window = auxiliary_window("캘린더", width, height)
        content = NSView.alloc().initWithFrame_(frame(0, 0, width, height))
        self._window.setContentView_(glass_container(content, width, height))
        label(content, "캘린더", 82, height - 48, 90, size=20, bold=True)
        card(content, 16, 50, 154, height - 124)
        label(content, "표시할 항목", 28, height - 108, 130, size=12, bold=True)
        self.filters = CalendarFilters(content, 28, height - 148, toggle, viewport=height - 182)
        grid_width = 560.0
        self.toolbar = CalendarToolbar(
            content, 190, height - 54, grid_width, controller, compact=False
        )
        card(content, 182, 50, 576, height - 124)
        row_height = (height - 162) / 6
        self.grid = CalendarGrid(
            content, 190, 60, grid_width, row_height, controller, compact=False
        )
        card(content, 770, 50, 294, height - 124)
        self._day = label(content, "", 786, height - 108, 256, size=15, bold=True)
        scroll = NSScrollView.alloc().initWithFrame_(frame(778, 62, 278, height - 194))
        scroll.setHasVerticalScroller_(True)
        scroll.setAutohidesScrollers_(True)
        scroll.setDrawsBackground_(False)
        scroll.setBorderType_(NSNoBorder)
        content.addSubview_(scroll)
        self.agenda = CalendarAgenda(scroll, controller, open_item)
        self._status = label(content, "", 190, 20, 850, size=12, secondary=True)

    def show(self) -> None:
        """메인 표시가 꺼져 있어도 독립적으로 현재 월을 연다."""
        if not self.controller.busy:
            self.controller.refresh()
        self.render(True)
        NSApplication.sharedApplication().activateIgnoringOtherApps_(True)
        self._window.makeKeyAndOrderFront_(None)

    def is_visible(self) -> bool:
        """보이지 않는 창 때문에 자동 조회를 유지하지 않는다."""
        return self._window.isVisible()

    def close(self) -> None:
        """창만 닫고 다음 열기의 날짜와 표시 선택은 보존한다."""
        self._window.close()

    def render(self, enabled: bool) -> None:
        """현재 상태를 두 화면에 같은 날짜·필터로 표시한다."""
        preferences = self.preferences()
        self.filters.update_choices(self._choices())
        self.filters.render(preferences, enabled)
        self.toolbar.render()
        self.grid.render(preferences)
        self.agenda.render(preferences)
        day = self.controller.selected
        self._day.setStringValue_(
            f"{day.month}월 {day.day}일 {('월', '화', '수', '목', '금', '토', '일')[day.weekday()]}요일"
            if day
            else "날짜를 선택하세요."
        )
        self._status.setStringValue_(self.controller.message)
        self._status.setToolTip_(self.controller.message or None)
