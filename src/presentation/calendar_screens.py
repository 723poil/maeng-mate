"""메뉴바와 메인 창의 달력 표시 상태와 사용자 조작을 조정한다."""

from collections.abc import Callable
from dataclasses import replace

from AppKit import NSView

from src.domain.calendar import CALENDAR_KINDS, CalendarPreferences
from src.presentation.calendar_controller import CalendarController
from src.presentation.calendar_controls import CalendarFilterChoice, calendar_filter_choices
from src.presentation.calendar_views import CalendarPage, CompactCalendarView
from src.presentation.main_window import MainWindow
from src.presentation.preferences_controller import PreferencesController


class CalendarScreens:
    """공유 컨트롤러의 조회 결과와 필터 변경을 보이는 달력에 반영한다."""

    def __init__(
        self,
        controller: CalendarController | None,
        preferences: PreferencesController,
        panel_visible: Callable[[], bool],
        window: Callable[[], MainWindow | None],
        open_calendar: Callable[[], None],
        open_item: Callable[[str | None], None],
    ) -> None:
        """창과 패널은 생성·페이지 전환 뒤의 현재 상태를 조회한다."""
        self.controller = controller
        self._preferences_controller = preferences
        self._compact_calendar_visible = panel_visible
        self._window = window
        self._open_calendar = open_calendar
        self._open_item = open_item
        self.compact: CompactCalendarView | None = None
        self.choices = calendar_filter_choices(())

    def compact_view(
        self, parent: NSView, width: float, *, inset: float = 0
    ) -> CompactCalendarView:
        """홈과 패널의 여백만 달리하고 같은 선택과 필터를 연결한다."""
        assert self.controller is not None
        return CompactCalendarView(
            parent,
            width,
            self.controller,
            self._calendar_preferences,
            self._toggle_calendar_filter,
            self._open_calendar,
            inset=inset,
            choices=self._calendar_filter_choices,
        )

    def month_view(self, parent: NSView) -> CalendarPage:
        """월간 창도 축약 달력과 같은 조회 상태를 읽는다."""
        assert self.controller is not None
        return CalendarPage(
            parent,
            self.controller,
            self._calendar_preferences,
            self._toggle_calendar_filter,
            self._open_item,
            choices=self._calendar_filter_choices,
        )

    def tick(self) -> None:
        """보이는 화면이 있을 때만 자동 조회를 시작한다."""
        if self.controller is not None:
            self.controller.tick(
                visible=self._compact_calendar_visible() or self._main_calendar_visible()
            )
            self.render()

    def loaded(self) -> None:
        """메인 스레드에서 새 응답만 반영한다."""
        if self.controller is not None and self.controller.apply_completed():
            self.render()

    def refresh(self) -> None:
        """중복 조회를 피하고 수동 조회 상태를 즉시 표시한다."""
        if self.controller is not None and not self.controller.busy:
            self.controller.refresh()
            self.render()

    def records_saved(self) -> None:
        """기록 정정 뒤 다음 표시 시 현재 월을 다시 읽는다."""
        if self.controller is not None:
            self.controller.invalidate()

    def _calendar_preferences(self) -> CalendarPreferences:
        """저장 중에도 두 달력에서 최신 표시 선택을 사용한다."""
        value = self._preferences_controller.value
        return value.calendar if value is not None else CalendarPreferences()

    def _calendar_filter_choices(self) -> tuple[CalendarFilterChoice, ...]:
        """두 달력에 같은 카테고리 선택지를 제공한다."""
        return self.choices

    def _toggle_calendar_filter(self, key: str, visible: bool) -> None:
        """카테고리와 출근 기록 표시만 저장한다."""
        value = self._preferences_controller.value
        if value is None or key not in {item.key for item in self._calendar_filter_choices()}:
            return
        preferences = value.calendar
        if key == "attendance":
            selected = set(preferences.visible_kinds)
            if visible:
                selected.add("attendance")
            else:
                selected.discard("attendance")
            preferences = replace(
                preferences,
                visible_kinds=tuple(item for item in CALENDAR_KINDS if item in selected),
            )
        else:
            hidden = set(preferences.hidden_categories)
            shown = set(preferences.shown_categories)
            if visible:
                hidden.discard(key)
                shown.add(key)
            else:
                hidden.add(key)
                shown.discard(key)
            preferences = replace(
                preferences,
                hidden_categories=tuple(sorted(hidden)),
                shown_categories=tuple(sorted(shown)),
            )
        self._preferences_controller.change(replace(value, calendar=preferences))
        self.render()

    def render(self) -> None:
        """필터 입력과 조회 진행을 두 화면에서 즉시 반영한다."""
        enabled = (
            self._preferences_controller.value is not None
            and not self._preferences_controller.loading
            and self._preferences_controller.error is None
        )
        if self.compact is not None and self._compact_calendar_visible():
            self.compact.render(enabled)
        window = self._window()
        if window is not None and window.window.isVisible():
            if window.calendar and window.page:
                window.page.render(enabled)
            elif not window.calendar and window.compact:
                window.compact.render(enabled)

    def _main_calendar_visible(self) -> bool:
        """홈 달력이나 월간 화면이 실제로 보이는 동안만 자동 조회한다."""
        window = self._window()
        return bool(window and window.window.isVisible() and (window.calendar or window.compact))
