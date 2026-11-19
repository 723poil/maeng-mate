"""근무 규칙을 조합해 저장하는 네이티브 폼이다."""

from dataclasses import dataclass

from AppKit import NSButton, NSScrollView, NSTextField, NSView
from Foundation import NSNotification, NSObject

from src.application.work_rules_settings import WorkRulesDocument, WorkRulesState
from src.domain.daily_work import DEFAULT_WORK_RULES, MAX_EXTRA_BREAKS, WorkRules, parse_rules
from src.presentation.native_views import button, card, frame, label
from src.presentation.runtime_settings_controller import SettingsEditor

type WorkRulesEditor = SettingsEditor[WorkRulesDocument, WorkRulesState]
WEEKDAYS = ("월", "화", "수", "목", "금", "토", "일")


@dataclass(frozen=True)
class BreakRow:
    """한 휴게 구간이 시작·종료와 삭제 버튼을 함께 가진다."""

    view: NSView
    start: NSTextField
    end: NSTextField
    remove: NSButton


class WorkRulesActions(NSObject):
    """규칙 수정과 휴게 행 추가·삭제를 연결한다."""

    owner: "WorkRulesForm"

    def changed_(self, sender: object) -> None:
        """요일과 계산 옵션은 규칙 초안을 변경한다."""
        self.owner.controller.edited()

    def addBreak_(self, sender: object) -> None:
        """필요할 때 추가 휴게 입력 한 줄을 연다."""
        self.owner.add_break()

    def removeBreak_(self, sender: NSButton) -> None:
        """선택한 휴게 행만 초안에서 제거한다."""
        self.owner.remove_break(sender.tag())

    def defaults_(self, sender: object) -> None:
        """기본값으로 초안만 바꿔 사용자가 저장하기 전 확인하게 한다."""
        self.owner.fill(DEFAULT_WORK_RULES)
        self.changed_(sender)

    def controlTextDidChange_(self, event: NSNotification) -> None:
        """문자 입력을 저장할 초안으로 표시한다."""
        self.owner.controller.edited()


class WorkRulesForm:
    """요일·시간·휴게·휴가 옵션을 세 카드에 모은다."""

    def __init__(self, page: NSScrollView, viewport: float, controller: WorkRulesEditor) -> None:
        """기존 설정 카드와 네이티브 입력으로 편집 영역을 구성한다."""
        self.controller = controller
        self._generation = -1
        self._display_state: tuple[bool, int] | None = None
        self._actions = WorkRulesActions.alloc().init()
        self._actions.owner = self
        self._page = page
        self._viewport = viewport
        self._break_count = 0
        self._controls: list[NSButton | NSTextField] = []
        self.document = NSView.alloc().initWithFrame_(frame(0, 0, 584, viewport))
        page.setDocumentView_(self.document)

        self._hours = self._section()
        card(self._hours, 0, 0, 552, 142)
        label(self._hours, "근무일과 출근", 12, 110, 250, bold=True)
        defaults = button(self._hours, "기본값", 446, 106, 92, self._actions, "defaults:")
        self._controls.append(defaults)
        self.days = tuple(
            self._checkbox(self._hours, day, 12 + index * 62, 77, 58)
            for index, day in enumerate(WEEKDAYS)
        )
        label(self._hours, "하루 실근무 (분)", 12, 46, 124)
        self.daily = self._field(self._hours, 139, 42, 75, "분")
        label(self._hours, "출근 범위", 256, 46, 70)
        self.arrival_start, self.arrival_end = self._range(self._hours, 339, 42)
        self.count_early = self._checkbox(
            self._hours, "출근 범위보다 일찍 출근한 시간도 근무로 계산", 12, 11, 460
        )

        self._rest = self._section()
        self._rest_card = card(self._rest, 0, 0, 552, 108)
        self._rest_header = NSView.alloc().initWithFrame_(frame(0, 0, 552, 108))
        self._rest.addSubview_(self._rest_header)
        label(self._rest_header, "휴게 구간", 12, 76, 228, bold=True)
        label(self._rest_header, "점심", 12, 45, 66)
        self.lunch_start, self.lunch_end = self._range(self._rest_header, 87, 41)
        label(self._rest_header, "추가 휴게", 12, 15, 96)
        self._add_break = button(self._rest_header, "+", 498, 10, 40, self._actions, "addBreak:")
        self._add_break.setAccessibilityLabel_("휴게 시간 추가")
        self.breaks = tuple(self._break_row(index) for index in range(MAX_EXTRA_BREAKS))

        self._leave = self._section()
        card(self._leave, 0, 0, 552, 80)
        label(self._leave, "휴가 차감", 12, 56, 120, bold=True)
        label(self._leave, "반차 (분)", 135, 55, 61)
        self.half = self._field(self._leave, 202, 52, 64, "분")
        label(self._leave, "반반차 (분)", 294, 55, 69)
        self.quarter = self._field(self._leave, 371, 52, 64, "분")
        self.early_half = self._checkbox(
            self._leave,
            "오후 반차로 점심시간이 끝나기 전에 퇴근할 때는 점심시간을 더하지 않음",
            12,
            13,
            522,
        )

        self.fill(DEFAULT_WORK_RULES)
        self.render()

    def _section(self) -> NSView:
        """각 카드의 내부 배치를 유지하며 카드 위치만 이동한다."""
        view = NSView.alloc().initWithFrame_(frame(0, 0, 552, 0))
        self.document.addSubview_(view)
        return view

    def _break_row(self, index: int) -> BreakRow:
        """추가를 누르기 전까지 숨겨 둘 입력 한 줄을 만든다."""
        view = NSView.alloc().initWithFrame_(frame(0, 0, 552, 30))
        self._rest.addSubview_(view)
        start, end = self._range(view, 87, 3)
        remove = button(view, "−", 498, 2, 40, self._actions, "removeBreak:")
        remove.setTag_(index)
        remove.setAccessibilityLabel_(f"추가 휴게 {index + 1} 삭제")
        self._controls.append(remove)
        return BreakRow(view, start, end, remove)

    def _layout(self) -> None:
        """추가 휴게 수에 맞춰 카드 높이를 바꾸고 스크롤 기준을 유지한다."""
        old_height = self.document.frame()[1][1]
        old_origin = self._page.contentView().bounds()[0][1]
        extra = self._break_count * 30
        rest_height = 108 + extra
        height = max(self._viewport, 150 + 12 + rest_height + 12 + 80 + 8)
        self.document.setFrameSize_((584, height))
        self._hours.setFrame_(frame(16, height - 150, 552, 142))
        rest_y = height - 162 - rest_height
        self._rest.setFrame_(frame(16, rest_y, 552, rest_height))
        self._rest_card.setFrame_(frame(0, 0, 552, rest_height))
        self._rest_header.setFrame_(frame(0, extra, 552, 108))
        for index, row in enumerate(self.breaks):
            row.view.setHidden_(index >= self._break_count)
            if index < self._break_count:
                row.view.setFrame_(frame(0, extra - (index + 1) * 30 + 6, 552, 30))
        self._leave.setFrame_(frame(16, rest_y - 92, 552, 80))
        self.document.scrollPoint_(
            (0, max(0, min(height - self._viewport, old_origin + height - old_height)))
        )
        self._update_add_button()

    def _update_add_button(self) -> None:
        """허용한 개수까지만 행을 추가할 수 있게 한다."""
        self._add_break.setEnabled_(
            self.controller.editable and self._break_count < MAX_EXTRA_BREAKS
        )

    def add_break(self) -> None:
        """추가한 행은 빈 초안으로 시작한다."""
        if not self.controller.editable or self._break_count >= MAX_EXTRA_BREAKS:
            return
        row = self.breaks[self._break_count]
        row.start.setStringValue_("")
        row.end.setStringValue_("")
        self._break_count += 1
        self.controller.edited()
        self._layout()

    def remove_break(self, index: int) -> None:
        """삭제한 행 뒤의 입력을 보존하며 순서만 당긴다."""
        if not self.controller.editable or not 0 <= index < self._break_count:
            return
        for position in range(index, self._break_count - 1):
            row, following = self.breaks[position : position + 2]
            row.start.setStringValue_(following.start.stringValue())
            row.end.setStringValue_(following.end.stringValue())
        self._break_count -= 1
        self.controller.edited()
        self._layout()

    def _field(self, parent: NSView, x: float, y: float, width: float, hint: str) -> NSTextField:
        """미입력 상태에서는 실제 기본 형식을 자리표시자로 안내한다."""
        field = NSTextField.textFieldWithString_("")
        field.setFrame_(frame(x, y, width, 24))
        field.setPlaceholderString_(hint)
        field.setDelegate_(self._actions)
        parent.addSubview_(field)
        self._controls.append(field)
        return field

    def _range(self, parent: NSView, x: float, y: float) -> tuple[NSTextField, NSTextField]:
        """동일한 너비로 시작과 끝을 한 줄에 놓는다."""
        start = self._field(parent, x, y, 74, "시작")
        label(parent, "–", x + 78, y + 4, 14, secondary=True)
        return start, self._field(parent, x + 92, y, 74, "종료")

    def _checkbox(self, parent: NSView, text: str, x: float, y: float, width: float) -> NSButton:
        """계산 옵션을 시스템 선택 컨트롤로 표시한다."""
        control = NSButton.checkboxWithTitle_target_action_(text, self._actions, "changed:")
        control.setFrame_(frame(x, y, width, 24))
        parent.addSubview_(control)
        self._controls.append(control)
        return control

    def fill(self, rules: WorkRules) -> None:
        """조회 결과 또는 기본값으로만 입력을 채운다."""
        self.daily.setStringValue_(str(rules.daily_minutes))
        self.half.setStringValue_(str(rules.half_minutes))
        self.quarter.setStringValue_(str(rules.quarter_minutes))
        self.arrival_start.setStringValue_(rules.arrival_start.strftime("%H:%M"))
        self.arrival_end.setStringValue_(rules.arrival_end.strftime("%H:%M"))
        self.lunch_start.setStringValue_(
            rules.lunch_start.strftime("%H:%M") if rules.lunch_start else ""
        )
        self.lunch_end.setStringValue_(rules.lunch_end.strftime("%H:%M") if rules.lunch_end else "")
        self._break_count = len(rules.extra_breaks)
        for index, row in enumerate(self.breaks):
            window = rules.extra_breaks[index] if index < len(rules.extra_breaks) else None
            row.start.setStringValue_(window.start.strftime("%H:%M") if window else "")
            row.end.setStringValue_(window.end.strftime("%H:%M") if window else "")
        for index, day in enumerate(self.days):
            day.setState_(int(index in rules.workdays))
        self.count_early.setState_(int(rules.count_early_arrival))
        self.early_half.setState_(int(rules.early_half_departure))
        self._layout()

    def draft(self) -> WorkRules:
        """검증된 규칙만 저장과 계산에 사용한다."""
        try:
            daily = int(self.daily.stringValue())
            half = int(self.half.stringValue())
            quarter = int(self.quarter.stringValue())
        except ValueError:
            raise ValueError("근무·휴가 시간은 소수점 없이 분 단위로 입력하세요.") from None
        extra = [
            {"start": row.start.stringValue().strip(), "end": row.end.stringValue().strip()}
            for row in self.breaks[: self._break_count]
        ]
        rules = parse_rules(
            {
                "dailyMinutes": daily,
                "halfMinutes": half,
                "quarterMinutes": quarter,
                "arrivalStart": self.arrival_start.stringValue().strip(),
                "arrivalEnd": self.arrival_end.stringValue().strip(),
                "lunchStart": self.lunch_start.stringValue().strip() or None,
                "lunchEnd": self.lunch_end.stringValue().strip() or None,
                "workdays": [index for index, day in enumerate(self.days) if day.state() == 1],
                "countEarlyArrival": self.count_early.state() == 1,
                "earlyHalfDeparture": self.early_half.state() == 1,
                "extraBreaks": extra,
            }
        )
        assert rules is not None
        return rules

    def render(self) -> None:
        """동일한 조회 응답으로 초안과 입력 초점을 덮지 않는다."""
        current = (self.controller.editable, self.controller.generation)
        if current == self._display_state:
            return
        self._display_state = current
        state = self.controller.state
        if state is not None and self._generation != self.controller.generation:
            self._generation = self.controller.generation
            self.fill(state.settings.rules)
        for control in self._controls:
            control.setEnabled_(self.controller.editable)
        self._update_add_button()
