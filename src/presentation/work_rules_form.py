"""근무 규칙을 조합해 저장하는 네이티브 폼이다."""

from dataclasses import dataclass

from AppKit import (
    NSButton,
    NSScrollView,
    NSTextField,
    NSView,
)
from Foundation import NSNotification, NSObject

from src.application.work_rules_settings import WorkRulesDocument, WorkRulesState
from src.domain.daily_work import DEFAULT_WORK_RULES, MAX_EXTRA_BREAKS, WorkRules, parse_rules
from src.presentation.native_views import (
    BUTTON_BEZEL_INSET,
    CARD_GAP,
    CARD_PADDING,
    CARD_TITLE,
    CARD_TITLE_GAP,
    CARD_WIDTH,
    CHIP_GAP,
    FORM_CONTROL_X,
    FORM_LABEL_WIDTH,
    FORM_ROW,
    FORM_ROW_GAP,
    Chip,
    button,
    card,
    card_height,
    form_control_y,
    form_divider,
    form_dividers,
    form_row_y,
    frame,
    icon_button,
    label,
    place_label,
)
from src.presentation.runtime_settings_controller import SettingsEditor

type WorkRulesEditor = SettingsEditor[WorkRulesDocument, WorkRulesState]
WEEKDAYS = ("월", "화", "수", "목", "금", "토", "일")
# 요일 칩은 공통 칩 규격을 쓰고 일곱 칸이 고르게 보이도록 폭만 고정한다.
DAY_WIDTH = 28.0
# 숫자·시각 입력칸 폭과 기본 입력 높이다.
NUMBER_WIDTH = 64.0
TIME_WIDTH = 64.0
FIELD_HEIGHT = 22.0
# 페이지 맨 위 카드와 스크롤 영역 위쪽 사이 여백이다.
PAGE_TOP = 8.0
# 휴게 카드에서 제목과 점심 줄을 묶은 머리 높이다.
REST_HEADER = CARD_PADDING + CARD_TITLE + CARD_TITLE_GAP + FORM_ROW


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

    def toggleDay_(self, sender: NSButton) -> None:
        """누른 요일만 근무일에서 넣거나 뺀다."""
        self.owner.toggle_day(sender.tag())

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
        self._workdays: set[int] = set()
        self.document = NSView.alloc().initWithFrame_(frame(0, 0, 584, viewport))
        page.setDocumentView_(self.document)

        hours_height = card_height(4)
        self._hours = self._section()
        card(self._hours, 0, 0, CARD_WIDTH, hours_height)
        form_dividers(self._hours, hours_height, 4)
        self._title(self._hours, "근무일과 출근", hours_height)
        defaults = button(
            self._hours,
            "기본값",
            CARD_WIDTH - CARD_PADDING - 72 + BUTTON_BEZEL_INSET,
            hours_height - CARD_PADDING - CARD_TITLE - 3,
            72,
            self._actions,
            "defaults:",
        )
        self._controls.append(defaults)
        row = form_row_y(hours_height, 0)
        self._row_label(self._hours, "근무 요일", row)
        # 요일은 둥근 사각형 버튼을 한 줄로 두고 누른 날만 강조색으로 채운다.
        self.days = tuple(self._day_toggle(index, day, row) for index, day in enumerate(WEEKDAYS))
        row = form_row_y(hours_height, 1)
        self._row_label(self._hours, "하루 실근무", row)
        self.daily = self._number(self._hours, row)
        row = form_row_y(hours_height, 2)
        self._row_label(self._hours, "출근 범위", row)
        self.arrival_start, self.arrival_end = self._range(self._hours, row)
        self.count_early = self._checkbox(
            self._hours, "일찍 출근한 시간도 근무에 포함", form_row_y(hours_height, 3)
        )

        self._rest = self._section()
        self._rest_card = card(self._rest, 0, 0, CARD_WIDTH, card_height(1))
        # 제목과 점심 줄은 한 묶음으로 카드 위쪽에 고정하고 추가 휴게 줄만 아래로 늘린다.
        self._rest_header = NSView.alloc().initWithFrame_(frame(0, 0, CARD_WIDTH, REST_HEADER))
        self._rest.addSubview_(self._rest_header)
        self._title(self._rest_header, "휴게", REST_HEADER)
        self._add_break = icon_button(
            self._rest_header,
            "plus",
            "휴게 추가",
            CARD_WIDTH - CARD_PADDING - 26,
            REST_HEADER - CARD_PADDING - CARD_TITLE - 3,
            self._actions,
            "addBreak:",
        )
        self._add_break.setFrame_(
            frame(
                CARD_WIDTH - CARD_PADDING - 26,
                REST_HEADER - CARD_PADDING - CARD_TITLE - 3,
                26,
                26,
            )
        )
        self._row_label(self._rest_header, "점심", 0)
        self.lunch_start, self.lunch_end = self._range(self._rest_header, 0)
        self.breaks = tuple(self._break_row(index) for index in range(MAX_EXTRA_BREAKS))
        # 점심과 추가 휴게 줄 사이 구분선은 휴게 수에 따라 위치만 옮긴다.
        self._rest_dividers = tuple(form_divider(self._rest, 0) for _ in range(MAX_EXTRA_BREAKS))

        # 반차·반반차를 한 줄에 두어 페이지가 스크롤 없이 한 화면에 들어오게 한다.
        leave_height = card_height(2)
        self._leave = self._section()
        card(self._leave, 0, 0, CARD_WIDTH, leave_height)
        form_dividers(self._leave, leave_height, 2)
        self._title(self._leave, "휴가 차감", leave_height)
        row = form_row_y(leave_height, 0)
        self._row_label(self._leave, "반차", row)
        self.half = self._number(self._leave, row)
        quarter_x = FORM_CONTROL_X + NUMBER_WIDTH + 48
        quarter_label = label(self._leave, "반반차", 0, 0, 44)
        place_label(quarter_label, quarter_x, row, 44, FORM_ROW)
        self.quarter = self._number(self._leave, row, x=quarter_x + 50)
        self.early_half = self._checkbox(
            self._leave, "오후 반차는 점심 전에 퇴근", form_row_y(leave_height, 1)
        )
        self.fill(DEFAULT_WORK_RULES)
        self.render()

    def _section(self) -> NSView:
        """각 카드의 내부 배치를 유지하며 카드 위치만 이동한다."""
        view = NSView.alloc().initWithFrame_(frame(0, 0, CARD_WIDTH, 0))
        self.document.addSubview_(view)
        return view

    def _title(self, parent: NSView, text: str, height: float) -> None:
        """카드 제목은 왼쪽 위 안쪽 여백에 같은 크기로 둔다."""
        title = label(parent, text, CARD_PADDING, 0, 300, size=13, bold=True)
        place_label(title, CARD_PADDING, height - CARD_PADDING - CARD_TITLE, 300, CARD_TITLE)

    def _row_label(self, parent: NSView, text: str, y: float) -> None:
        """항목 이름은 왼쪽 칸에서 입력 줄 높이의 가운데에 둔다."""
        name = label(parent, text, CARD_PADDING, 0, FORM_LABEL_WIDTH - 8)
        place_label(name, CARD_PADDING, y, FORM_LABEL_WIDTH - 8, FORM_ROW)

    def _break_row(self, index: int) -> BreakRow:
        """추가를 누르기 전까지 숨겨 둘 입력 한 줄을 만든다."""
        view = NSView.alloc().initWithFrame_(frame(0, 0, CARD_WIDTH, FORM_ROW))
        self._rest.addSubview_(view)
        self._row_label(view, f"휴게 {index + 1}", 0)
        start, end = self._range(view, 0)
        remove = icon_button(
            view,
            "minus.circle",
            f"휴게 {index + 1} 삭제",
            CARD_WIDTH - CARD_PADDING - 26,
            form_control_y(0, 26),
            self._actions,
            "removeBreak:",
        )
        remove.setFrame_(frame(CARD_WIDTH - CARD_PADDING - 26, form_control_y(0, 26), 26, 26))
        remove.setTag_(index)
        self._controls.append(remove)
        return BreakRow(view, start, end, remove)

    def _layout(self) -> None:
        """추가 휴게 수에 맞춰 카드 높이를 바꾸고 스크롤 기준을 유지한다."""
        old_height = self.document.frame()[1][1]
        old_origin = self._page.contentView().bounds()[0][1]
        hours_height, leave_height = card_height(4), card_height(2)
        rest_height = card_height(1 + self._break_count)
        content = PAGE_TOP + hours_height + CARD_GAP + rest_height + CARD_GAP + leave_height
        # 마지막 카드 아래에는 카드 사이 간격만 두어 기본 구성이 스크롤 없이 들어오게 한다.
        height = max(self._viewport, content + CARD_GAP)
        self.document.setFrameSize_((584, height))
        top = height - PAGE_TOP
        self._hours.setFrame_(frame(CARD_PADDING, top - hours_height, CARD_WIDTH, hours_height))
        top -= hours_height + CARD_GAP
        self._rest.setFrame_(frame(CARD_PADDING, top - rest_height, CARD_WIDTH, rest_height))
        self._rest_card.setFrame_(frame(0, 0, CARD_WIDTH, rest_height))
        self._rest_header.setFrame_(frame(0, rest_height - REST_HEADER, CARD_WIDTH, REST_HEADER))
        for index, divider in enumerate(self._rest_dividers):
            divider.setHidden_(index >= self._break_count)
            divider.setFrame_(
                frame(
                    CARD_PADDING,
                    form_row_y(rest_height, index) - FORM_ROW_GAP / 2,
                    CARD_WIDTH - 2 * CARD_PADDING,
                    1,
                )
            )
        for index, row in enumerate(self.breaks):
            row.view.setHidden_(index >= self._break_count)
            if index < self._break_count:
                row.view.setFrame_(
                    frame(0, form_row_y(rest_height, index + 1), CARD_WIDTH, FORM_ROW)
                )
        top -= rest_height + CARD_GAP
        self._leave.setFrame_(frame(CARD_PADDING, top - leave_height, CARD_WIDTH, leave_height))
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
        """입력 줄 높이의 가운데에 기본 높이 입력칸을 둔다."""
        field = NSTextField.textFieldWithString_("")
        field.setFrame_(frame(x, y + (FORM_ROW - FIELD_HEIGHT) / 2, width, FIELD_HEIGHT))
        field.setPlaceholderString_(hint)
        field.setDelegate_(self._actions)
        parent.addSubview_(field)
        self._controls.append(field)
        return field

    def _number(self, parent: NSView, y: float, *, x: float = FORM_CONTROL_X) -> NSTextField:
        """분 단위 숫자 입력 뒤에 단위를 붙인다."""
        field = self._field(parent, x, y, NUMBER_WIDTH, "")
        unit = label(parent, "분", 0, 0, 24, secondary=True)
        place_label(unit, x + NUMBER_WIDTH + 6, y, 24, FORM_ROW)
        return field

    def _range(self, parent: NSView, y: float) -> tuple[NSTextField, NSTextField]:
        """동일한 너비로 시작과 끝을 한 줄에 놓는다."""
        start = self._field(parent, FORM_CONTROL_X, y, TIME_WIDTH, "시작")
        dash = label(parent, "–", 0, 0, 12, secondary=True)
        place_label(dash, FORM_CONTROL_X + TIME_WIDTH + 6, y, 12, FORM_ROW)
        end = self._field(parent, FORM_CONTROL_X + TIME_WIDTH + 24, y, TIME_WIDTH, "종료")
        return start, end

    def _day_toggle(self, index: int, title: str, y: float) -> Chip:
        """요일 하나를 공통 선택 칩으로 만든다."""
        x = FORM_CONTROL_X + index * (DAY_WIDTH + CHIP_GAP)
        chip = Chip(self._hours, title, x, y, self._actions, "toggleDay:", width=DAY_WIDTH)
        chip.button.setTag_(index)
        chip.button.setAccessibilityLabel_(f"{title}요일")
        self._controls.append(chip.button)
        return chip

    def toggle_day(self, index: int) -> None:
        """편집할 수 있을 때만 요일 선택을 바꾸고 초안으로 표시한다."""
        if not self.controller.editable:
            return
        self._workdays ^= {index}
        self._render_days()
        self.controller.edited()

    def _render_days(self) -> None:
        """켜진 요일만 강조색 칩으로 그린다."""
        for index, chip in enumerate(self.days):
            chip.set_selected(index in self._workdays)

    def _checkbox(self, parent: NSView, text: str, y: float) -> NSButton:
        """계산 옵션은 입력 칸과 같은 x에서 시작하는 시스템 체크박스로 표시한다."""
        control = NSButton.checkboxWithTitle_target_action_(text, self._actions, "changed:")
        width = CARD_WIDTH - FORM_CONTROL_X - CARD_PADDING
        control.setFrame_(frame(FORM_CONTROL_X, y + (FORM_ROW - 24) / 2, width, 24))
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
        self._workdays = set(rules.workdays)
        self._render_days()
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
                "workdays": sorted(self._workdays),
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
