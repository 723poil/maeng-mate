"""메인의 작은 달력과 Google Calendar식 별도 월 창을 구성한다."""

from collections.abc import Callable

from AppKit import (
    NSColor,
    NSLineBreakByTruncatingTail,
    NSNoBorder,
    NSScrollView,
    NSView,
)

from src.domain.calendar import CalendarPreferences
from src.presentation.calendar_agenda import CalendarAgenda
from src.presentation.calendar_controller import CalendarController
from src.presentation.calendar_controls import (
    CalendarFilterChoice,
    CalendarToolbar,
    calendar_filter_choices,
)
from src.presentation.calendar_filter_popover import CalendarFilterPopover
from src.presentation.calendar_grid import CalendarGrid
from src.presentation.calendar_style import attendance_hint, attendance_summary, item_summary
from src.presentation.native_views import card, frame, label

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
        inset: float = 16,
        choices: Callable[[], tuple[CalendarFilterChoice, ...]] = lambda: calendar_filter_choices(
            ()
        ),
    ) -> None:
        """메인 영역 이동에서도 컨트롤을 새로 만들지 않는다."""
        self.controller, self.preferences = controller, preferences
        self._inset = inset
        content_width = width - 2 * inset
        self.view = NSView.alloc().initWithFrame_(
            frame(inset, 0, content_width, COMPACT_CALENDAR_HEIGHT)
        )
        parent.addSubview_(self.view)
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
        self._day = label(self.view, "", 14, 48, content_width - 28, size=12, bold=True)
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
        self.view.setFrame_(frame(self._inset, y, self.view.frame()[1][0], COMPACT_CALENDAR_HEIGHT))

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
        weekday = ("월", "화", "수", "목", "금", "토", "일")[day.weekday()]
        # 일정 수는 아래 줄에서 바로 보이므로 제목에는 날짜만 둔다.
        self._day.setStringValue_(f"{day.month}월 {day.day}일 {weekday}요일")
        lines = [item_summary(item, day) for item in items[:2]]
        placeholder = not lines and record is None
        if lines:
            self._first.setStringValue_(lines[0])
            self._first.setToolTip_(lines[0])
        else:
            # 일정이 없으면 조회 안내(연결 없음·실패)를, 그것도 없으면 짧은 빈 표시를 쓴다.
            self._first.setStringValue_(
                attendance_summary(record) if record else self.controller.message or "일정 없음"
            )
            self._first.setToolTip_(attendance_hint(record) if record else None)
        self._first.setTextColor_(
            NSColor.secondaryLabelColor() if placeholder else NSColor.labelColor()
        )
        self._second.setStringValue_(lines[1] if len(lines) > 1 else "")


class CalendarPage:
    """메인 창 안에 월 격자와 선택 날짜 상세를 표시한다."""

    def __init__(
        self,
        content: NSView,
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
        width, height = content.frame()[1]
        self.filter_popover = CalendarFilterPopover(preferences, toggle, choices)
        detail_width = min(264.0, width / 3)
        grid_width = width - detail_width - 24
        self.toolbar = CalendarToolbar(
            content,
            8,
            height - 36,
            grid_width,
            controller,
            compact=False,
            open_filters=self.filter_popover.show,
        )
        card(content, 0, 28, grid_width + 12, height - 72)
        self.grid = CalendarGrid(
            content, 6, 36, grid_width, (height - 112) / 6, controller, compact=False
        )
        x = width - detail_width
        card(content, x, 28, detail_width, height - 72)
        self._day = label(content, "", x + 8, height - 74, detail_width - 16, size=15, bold=True)
        scroll = NSScrollView.alloc().initWithFrame_(
            frame(x + 6, 34, detail_width - 12, height - 120)
        )
        scroll.setHasVerticalScroller_(True)
        scroll.setAutohidesScrollers_(True)
        scroll.setDrawsBackground_(False)
        scroll.setBorderType_(NSNoBorder)
        content.addSubview_(scroll)
        self.agenda = CalendarAgenda(scroll, controller, open_item)
        self._status = label(content, "", 8, 4, width - 16, size=12, secondary=True)

    def render(self, enabled: bool) -> None:
        """현재 상태를 두 화면에 같은 날짜·필터로 표시한다."""
        preferences = self.preferences()
        self.filter_popover.render(enabled)
        if self.toolbar.filter_button is not None:
            self.toolbar.filter_button.setEnabled_(enabled)
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
