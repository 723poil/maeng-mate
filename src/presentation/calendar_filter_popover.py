"""메인 달력의 표시 항목을 클릭으로 선택하는 네이티브 팝오버다."""

from collections.abc import Callable

from AppKit import NSMinYEdge, NSPopover, NSPopoverBehaviorTransient, NSView, NSViewController

from src.domain.calendar import CalendarPreferences
from src.presentation.calendar_controls import CalendarFilterChoice, CalendarFilters
from src.presentation.native_views import card, frame, label


class CalendarFilterPopover:
    """필터 선택과 창 크기를 분리하고 바깥 클릭으로 닫는다."""

    def __init__(
        self,
        preferences: Callable[[], CalendarPreferences],
        toggle: Callable[[str, bool], None],
        choices: Callable[[], tuple[CalendarFilterChoice, ...]],
    ) -> None:
        """기존 설정 콜백을 재사용해 메인·별도 창 선택을 유지한다."""
        self._preferences = preferences
        self._choices = choices
        self._enabled = False
        self._opening = False
        width, height = 200.0, 270.0
        self.content = NSView.alloc().initWithFrame_(frame(0, 0, width, height))
        card(self.content, 0, 0, width, height)
        label(self.content, "표시 항목", 16, height - 34, width - 32, size=13, bold=True)
        self.filters = CalendarFilters(self.content, 16, height - 72, toggle, row_width=168)
        controller = NSViewController.alloc().init()
        controller.setView_(self.content)
        self.popover = NSPopover.alloc().init()
        self.popover.setContentViewController_(controller)
        self.popover.setContentSize_((width, height))
        self.popover.setBehavior_(NSPopoverBehaviorTransient)

    @property
    def is_open(self) -> bool:
        """표시 전 키 전환 동안에도 팝오버 사용 중임을 알린다."""
        return self._opening or self.popover.isShown()

    def show(self, anchor: NSView) -> None:
        """같은 버튼은 닫기로 처리하고 현재 선택값으로 연다."""
        if self.is_open:
            self.hide()
            return
        if not self._enabled:
            return
        self.render(True)
        self._opening = True
        try:
            self.popover.showRelativeToRect_ofView_preferredEdge_(
                anchor.bounds(), anchor, NSMinYEdge
            )
        finally:
            self._opening = False

    def render(self, enabled: bool) -> None:
        """열린 상태에서도 다른 화면에서 바꾼 선택을 바로 반영한다."""
        self._enabled = enabled
        self.filters.update_choices(self._choices())
        self.filters.render(self._preferences(), enabled)

    def hide(self) -> None:
        """영역 숨김·부모 창 종료 때 선택 창도 정리한다."""
        self.popover.performClose_(None)
