"""달력의 월 이동과 종류별 표시 선택을 구성한다."""

from collections.abc import Callable
from dataclasses import dataclass

from AppKit import (
    NSBox,
    NSButton,
    NSColor,
    NSFont,
    NSImage,
    NSNoBorder,
    NSProgressIndicator,
    NSProgressIndicatorStyleSpinning,
    NSScrollView,
    NSTextAlignmentCenter,
    NSTextField,
    NSView,
)
from Foundation import NSObject

from src.domain.calendar import CalendarKind, CalendarPreferences
from src.domain.source_display import SourceDisplay, display_groups
from src.presentation.calendar_controller import CalendarController
from src.presentation.calendar_style import box, kind_color
from src.presentation.native_views import button, frame, label


class CalendarControlActions(NSObject):
    """같은 월 이동 동작을 두 화면에서 재사용한다."""

    owner: "CalendarToolbar"

    def previous_(self, sender: object) -> None:
        """이전 월로 이동한다."""
        self.owner.controller.move(-1)
        self.owner.render()

    def next_(self, sender: object) -> None:
        """다음 월로 이동한다."""
        self.owner.controller.move(1)
        self.owner.render()

    def today_(self, sender: object) -> None:
        """현재 월·오늘 날짜로 돌아간다."""
        self.owner.controller.today()
        self.owner.render()

    def refresh_(self, sender: object) -> None:
        """현재 저장된 월 자료를 다시 읽는다."""
        self.owner.controller.refresh()
        self.owner.render()

    def filters_(self, sender: NSButton) -> None:
        """선택 버튼 옆에서 표시 항목 팝오버를 연다."""
        if self.owner.open_filters is not None:
            self.owner.open_filters(sender)

    def open_(self, sender: object) -> None:
        """작은 달력과 같은 선택 날짜를 별도 창에서 연다."""
        if self.owner.open_window:
            self.owner.open_window()


class CalendarToolbar:
    """오늘·화살표·월 제목·새로고침을 같은 기능별 요소로 표시한다."""

    def __init__(
        self,
        parent: NSView,
        x: float,
        y: float,
        width: float,
        controller: CalendarController,
        *,
        compact: bool,
        open_window: Callable[[], None] | None = None,
        open_filters: Callable[[NSView], None] | None = None,
    ) -> None:
        """작은 카드와 큰 달력의 사용 가능한 너비에 맞춰 한 줄로 배치한다."""
        self.controller, self.open_window = controller, open_window
        self.open_filters = open_filters
        self.filter_button: NSButton | None = None
        self._compact = compact
        self._actions = CalendarControlActions.alloc().init()
        self._actions.owner = self
        if compact:
            label(parent, "캘린더", x + 12, y + 6, 52, size=13, bold=True)
            if open_filters is not None:
                self.filter_button = button(
                    parent, "", x + 70, y, 24, self._actions, "filters:", bordered=False
                )
                self.filter_button.setImage_(
                    NSImage.imageWithSystemSymbolName_accessibilityDescription_(
                        "line.3.horizontal.decrease", "표시 항목"
                    )
                )
                self.filter_button.setToolTip_("표시 항목")
            self.month = label(parent, "", x + 136, y + 6, 104, size=12, bold=True)
            self.month.setAlignment_(NSTextAlignmentCenter)
            coordinates = (104, 248, 278, 342)
        else:
            self.month = label(parent, "", x, y + 3, 154, size=20, bold=True)
            coordinates = (230, 266, 160, 308)
        self._previous = button(
            parent, "", x + coordinates[0], y, 24, self._actions, "previous:", bordered=False
        )
        self._next = button(
            parent, "", x + coordinates[1], y, 24, self._actions, "next:", bordered=False
        )
        self._today = button(parent, "오늘", x + coordinates[2], y, 58, self._actions, "today:")
        self._refresh = button(
            parent, "", x + coordinates[3], y, 24, self._actions, "refresh:", bordered=False
        )
        for control, icon, title in (
            (self._previous, "chevron.left", "이전 달"),
            (self._next, "chevron.right", "다음 달"),
            (self._refresh, "arrow.clockwise", "캘린더 새로고침"),
        ):
            control.setImage_(
                NSImage.imageWithSystemSymbolName_accessibilityDescription_(icon, title)
            )
            control.setToolTip_(title)
        if compact and open_window:
            button(parent, "창으로 보기 ↗", x + 388, y, 88, self._actions, "open:", bordered=False)
        self._spinner = NSProgressIndicator.alloc().initWithFrame_(
            frame(x + width - 20 if not compact else x + 370, y + 7, 12, 12)
        )
        self._spinner.setStyle_(NSProgressIndicatorStyleSpinning)
        self._spinner.setDisplayedWhenStopped_(False)
        parent.addSubview_(self._spinner)
        self.render()

    def render(self) -> None:
        """자료를 지우지 않고 조회 상태와 월 제목을 갱신한다."""
        data = self.controller.data
        self.month.setStringValue_(
            f"{int(data.month[:4])}년 {int(data.month[5:])}월" if data else ""
        )
        for control in (self._previous, self._next, self._today, self._refresh):
            control.setEnabled_(not self.controller.busy)
        if self.controller.busy:
            self._spinner.startAnimation_(None)
        else:
            self._spinner.stopAnimation_(None)


class FilterActions(NSObject):
    """표시 선택만 바꾸며 수집과 알림 선택은 변경하지 않는다."""

    owner: "CalendarFilters"

    def toggle_(self, sender: NSButton) -> None:
        """종류 키 하나만 확정 설정의 새 초안으로 전달한다."""
        index = sender.tag()
        if 0 <= index < len(self.owner.choices):
            self.owner.changed(self.owner.choices[index].key, sender.state() == 1)


class FilterLabel(NSTextField):
    """선택창 배경의 강조색과 분리된 읽기 쉬운 체크박스 라벨이다."""

    control: NSButton

    def mouseDownCanMoveWindow(self) -> bool:
        """라벨을 클릭하면 창 이동 대신 선택을 처리한다."""
        return False

    def mouseDown_(self, event: object) -> None:
        """라벨 클릭도 체크박스 클릭과 같은 동작을 실행한다."""
        if self.control.isEnabled():
            self.control.performClick_(None)


@dataclass(frozen=True)
class CalendarFilterChoice:
    """등록된 카테고리나 출근 기록을 달력의 선택 한 줄로 표시한다."""

    key: str
    title: str
    kind: CalendarKind
    color: str | None = None


def calendar_filter_choices(display: tuple[SourceDisplay, ...]) -> tuple[CalendarFilterChoice, ...]:
    """달력 표시를 켠 소스가 연결된 카테고리만 선택지로 만든다."""
    choices = [CalendarFilterChoice("attendance", "출근 기록", "attendance")]
    for key, group in display_groups(display).items():
        if item := next((source for source in group if source.calendar_visible), None):
            kind: CalendarKind = "qa" if item.section == "qa" else item.kind
            choices.append(CalendarFilterChoice(key, item.title, kind, item.color))
    return tuple(choices)


class CalendarFilters:
    """큰 창의 좌측 목록과 필터 팝오버를 같은 표시 설정에 연결한다."""

    def __init__(
        self,
        parent: NSView,
        x: float,
        y: float,
        changed: Callable[[str, bool], None],
        *,
        row_width: float = 128,
        viewport: float = 204,
    ) -> None:
        """종류별 색·기본 체크박스·라벨을 동일한 세로 간격으로 배치한다."""
        self.changed = changed
        self._actions = FilterActions.alloc().init()
        self._actions.owner = self
        self._width, self._viewport = row_width, viewport
        self.choices: tuple[CalendarFilterChoice, ...] = ()
        self.controls: dict[str, NSButton] = {}
        self._dots: dict[str, NSBox] = {}
        self._labels: dict[str, FilterLabel] = {}
        self._scroll = NSScrollView.alloc().initWithFrame_(
            frame(x, y + 26 - viewport, row_width, viewport)
        )
        self._scroll.setHasVerticalScroller_(True)
        self._scroll.setAutohidesScrollers_(True)
        self._scroll.setDrawsBackground_(False)
        self._scroll.setBorderType_(NSNoBorder)
        self._rows = NSView.alloc().initWithFrame_(frame(0, 0, row_width, viewport))
        self._scroll.setDocumentView_(self._rows)
        parent.addSubview_(self._scroll)

    def update_choices(self, choices: tuple[CalendarFilterChoice, ...]) -> None:
        """카테고리 목록이 바뀐 때만 행을 다시 배치한다."""
        if self.choices == choices:
            return
        self.choices = choices
        self._rows.setSubviews_([])
        self.controls.clear()
        self._labels.clear()
        self._dots.clear()
        height = max(self._viewport, len(choices) * 34)
        self._rows.setFrameSize_((self._width, height))
        for index, choice in enumerate(choices):
            top = height - 26 - index * 34
            control = NSButton.checkboxWithTitle_target_action_("", self._actions, "toggle:")
            control.setFrame_(frame(0, top, self._width - 14, 26))
            control.setTag_(index)
            control.setAccessibilityLabel_(choice.title)
            self._rows.addSubview_(control)
            self.controls[choice.key] = control
            caption = FilterLabel.alloc().initWithFrame_(frame(22, top + 4, self._width - 46, 18))
            caption.control = control
            caption.setStringValue_(choice.title)
            caption.setToolTip_(choice.title)
            caption.setEditable_(False)
            caption.setSelectable_(False)
            caption.setBezeled_(False)
            caption.setBordered_(False)
            caption.setDrawsBackground_(False)
            caption.setFont_(NSFont.systemFontOfSize_(13))
            self._rows.addSubview_(caption)
            self._labels[choice.key] = caption
            self._dots[choice.key] = box(
                self._rows,
                self._width - 21,
                top + 9,
                6,
                6,
                kind_color(choice.kind, choice.color),
                3,
            )
        self._rows.scrollPoint_((0, max(0, height - self._viewport)))

    def render(self, value: CalendarPreferences, enabled: bool) -> None:
        """선택창에서도 라벨 색과 선택 상태를 명확히 유지한다."""
        for choice in self.choices:
            control = self.controls[choice.key]
            visible = value.is_visible(choice.key)
            control.setState_(int(visible))
            control.setEnabled_(enabled)
            self._labels[choice.key].setTextColor_(
                NSColor.labelColor() if enabled else NSColor.secondaryLabelColor()
            )
            self._dots[choice.key].setFillColor_(
                kind_color(choice.kind, choice.color).colorWithAlphaComponent_(
                    1 if visible else 0.2
                )
            )
