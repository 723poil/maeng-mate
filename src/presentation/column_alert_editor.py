"""카테고리에 정의한 저장 칼럼과 직접 입력한 조건으로 알림을 작성한다."""

from collections.abc import Callable
from dataclasses import replace
from uuid import UUID, uuid4
from weakref import proxy

from AppKit import (
    NSAlert,
    NSAlertFirstButtonReturn,
    NSButton,
    NSColor,
    NSLineBreakByTruncatingTail,
    NSMenuItem,
    NSNoBorder,
    NSPopUpButton,
    NSScrollView,
    NSTableCellView,
    NSTableColumn,
    NSTableView,
    NSTableViewSelectionHighlightStyleSourceList,
    NSTableViewStyleSourceList,
    NSTextAlignmentRight,
    NSTextField,
    NSView,
)
from Foundation import NSIndexSet, NSObject

from src.application.errors import COLLECTION_FAILURE_CODES
from src.application.source_settings import SourceSettingsError
from src.domain.category_configuration import CategoryColumn
from src.domain.main_list import MainListRules
from src.domain.source_alerts import (
    COLLECTION_STATE,
    MAX_SOURCE_ALERT_RULES,
    UNBOUND_COLUMN,
    AlertDelivery,
    AlertSchedule,
    ScheduleMode,
    SourceAlertRule,
)
from src.presentation.main_list_editor import MainListEditor
from src.presentation.native_views import button, card, frame, label

RULE_ROW_HEIGHT = 42.0
RULE_LIST_WIDTH = 226.0
CHANGE_CONDITIONS = ("조건 선택", "값이 바뀔 때", "특정 값으로 바뀔 때", "값이 비워질 때")
SCHEDULE_CHOICES: tuple[tuple[ScheduleMode, str], ...] = (
    ("previous_day", "전날 지정 시각"),
    ("same_day", "당일 지정 시각"),
    ("before_start", "시작 시각 몇 분 전"),
)
DELIVERY_CHOICES = (
    ("선택 안 함", ()),
    ("macOS 알림", ("macos",)),
    ("Slack DM", ("slack",)),
    ("macOS 알림과 Slack DM", ("macos", "slack")),
)


class AlertRuleCell(NSTableCellView):
    """투명한 목록 셀에서도 창 이동보다 선택을 먼저 처리한다."""

    def mouseDownCanMoveWindow(self) -> bool:
        """셀 안을 클릭하면 선택에 사용한다."""
        return False


class AlertEditorActions(NSObject):
    """규칙 선택과 조건 입력을 폼의 상태에 연결한다."""

    owner: "ColumnAlertEditor"

    def numberOfRowsInTableView_(self, table: NSTableView) -> int:
        """목록은 작성된 규칙만 포함한다."""
        return self.owner.rule_count

    def tableView_viewForTableColumn_row_(
        self, table: NSTableView, column: NSTableColumn | None, row: int
    ) -> NSView | None:
        """규칙 이름과 조건을 한 행에서 읽을 수 있도록 구성한다."""
        return self.owner.rule_row(row)

    def tableViewSelectionDidChange_(self, notification: object) -> None:
        """클릭과 방향키 선택을 같은 편집기에 반영한다."""
        self.owner.select_rule(self.owner.rule_list.selectedRow())

    def newRule_(self, sender: object) -> None:
        """선택한 종류의 빈 규칙 편집기를 연다."""
        self.owner.open_new()

    def removeRule_(self, sender: object) -> None:
        """선택한 규칙을 설정 초안에서 뺀다."""
        self.owner.remove()

    def column_(self, sender: object) -> None:
        """선택한 카테고리 칼럼의 값 종류에 맞게 입력을 표시한다."""
        self.owner.change_column()

    def condition_(self, sender: object) -> None:
        """특정 값 조건에만 변경될 값의 입력을 받는다."""
        self.owner.render_condition()
        self.owner.input_changed()

    def scope_(self, sender: object) -> None:
        """현재 규칙에서 사용할 항목 조건을 편집한다."""
        self.owner.edit_scope()

    def target_(self, sender: object) -> None:
        """사용자가 후속 값을 선택하면 이전 오류를 지운다."""
        self.owner.input_changed()

    def controlTextDidChange_(self, notification: object) -> None:
        """이름이나 값을 편집할 때도 직전 오류를 지운다."""
        self.owner.input_changed()


class ColumnAlertEditor:
    """카테고리의 저장 칼럼으로 한 번에 규칙 하나를 편집한다."""

    def __init__(
        self,
        parent: NSView,
        columns: tuple[CategoryColumn, ...],
        rules: tuple[SourceAlertRule, ...],
        person_scoped: bool,
        clear_sheet_status: Callable[[], None],
        *,
        people_columns: frozenset[str] = frozenset(),
    ) -> None:
        """실제 카테고리 칼럼과 수집 상태를 입력으로 사용한다."""
        self._columns = (*columns, CategoryColumn(COLLECTION_STATE, "수집 상태", "collection"))
        self._people_columns = (
            people_columns
            | {c.id for c in columns if c.legacy_kind == "people"}
            | {r.column_id for r in rules if r.member}
        )
        self._rules = list(rules)
        self._index = 0
        self._preview: SourceAlertRule | None = None
        self._updating_list = False
        self._clear_sheet_status = clear_sheet_status
        self._actions = AlertEditorActions.alloc().init()
        self._actions.owner = proxy(self)
        self.view = parent
        self._scope = label(parent, "", 10, 348, 390, secondary=True)
        self._scope_rules: MainListRules | None = None
        self._scope_button = button(
            parent, "적용 대상 설정…", 400, 344, 228, self._actions, "scope:"
        )
        card(parent, 0, 41, 242, 290)
        label(parent, "알림 규칙", 14, 294, 105, bold=True)
        self._new = button(parent, "+ 새 규칙", 126, 290, 104, self._actions, "newRule:")
        self._empty = label(parent, "등록된 규칙이 없습니다.", 14, 249, 214, secondary=True)
        self.rule_list = self._make_list(parent)
        card(parent, 254, 41, 388, 290)
        self._editor_title = label(parent, "", 268, 294, 360, bold=True)
        label(parent, "알림 이름", 268, 261, 86)
        self.name = self._field(359, 257, 269, "알림에 표시할 이름")
        label(parent, "기준 데이터", 268, 230, 86)
        self.column = self._popup(359, 226, 269, "column:")
        self.condition = self._popup(359, 195, 269, "condition:")
        self._schedule_choices: tuple[tuple[ScheduleMode, str], ...] = ()
        label(parent, "언제 보낼지", 268, 199, 86)
        self._target_label = label(parent, "변경 후 값", 268, 168, 86)
        self.target = self._field(359, 164, 269, "원본의 값과 같게 입력")
        self._target_choice = self._popup(359, 164, 269, "target:")
        label(parent, "알림 문구", 268, 137, 86)
        self.message = self._field(359, 133, 269, "보낼 본문 입력")
        label(parent, "전달 채널", 268, 106, 86)
        self.delivery_channel = self._popup(359, 102, 269, "target:")
        self.delivery_channel.setToolTip_("전체 및 카테고리 설정에서 허용한 채널로 보냅니다.")
        self._target_values: tuple[str, ...] = ()
        self._visible_column: CategoryColumn | None = None
        self.enabled = NSButton.checkboxWithTitle_target_action_(
            "이 규칙 사용", self._actions, "condition:"
        )
        self.enabled.setFrame_(frame(268, 58, 183, 26))
        parent.addSubview_(self.enabled)
        for field in (self.name, self.target, self.message):
            field.setDelegate_(self._actions)
        self._remove = button(parent, "규칙 삭제", 522, 57, 106, self._actions, "removeRule:")
        self.status = label(parent, "", 14, 14, 610, secondary=True)
        self.rule_list.setDataSource_(self._actions)
        self.rule_list.setDelegate_(self._actions)
        self.set_person_scope(person_scoped)
        self._fill(0)

    @property
    def rule_count(self) -> int:
        """작성 중인 빈 입력은 규칙 수에 넣지 않는다."""
        return len(self._rules)

    def _make_list(self, parent: NSView) -> NSTableView:
        """규칙이 많아도 편집기를 밀어내지 않는 스크롤 목록을 만든다."""
        viewport = frame(8, 50, RULE_LIST_WIDTH, 228)
        scroll = NSScrollView.alloc().initWithFrame_(viewport)
        scroll.setBorderType_(NSNoBorder)
        scroll.setDrawsBackground_(False)
        scroll.setHasVerticalScroller_(True)
        scroll.setAutohidesScrollers_(True)
        table = NSTableView.alloc().initWithFrame_(frame(0, 0, RULE_LIST_WIDTH, 228))
        column = NSTableColumn.alloc().initWithIdentifier_("rule")
        column.setWidth_(RULE_LIST_WIDTH)
        table.addTableColumn_(column)
        table.setHeaderView_(None)
        table.setStyle_(NSTableViewStyleSourceList)
        table.setSelectionHighlightStyle_(NSTableViewSelectionHighlightStyleSourceList)
        table.setBackgroundColor_(NSColor.clearColor())
        table.setRowHeight_(RULE_ROW_HEIGHT)
        table.setIntercellSpacing_((0, 0))
        table.setAllowsMultipleSelection_(False)
        table.setAllowsEmptySelection_(True)
        scroll.setDocumentView_(table)
        parent.addSubview_(scroll)
        return table

    def rule_row(self, index: int) -> NSView | None:
        """등록한 이름·조건·켜짐 상태를 선택 가능한 한 행으로 표시한다."""
        if not 0 <= index < self.rule_count:
            return None
        rule = (
            self._preview
            if self._preview is not None and index == self._index
            else self._rules[index]
        )
        name, detail = rule.name, self._rule_summary(rule)
        state = "켜짐" if rule.enabled else "꺼짐"
        cell = AlertRuleCell.alloc().initWithFrame_(frame(0, 0, RULE_LIST_WIDTH, RULE_ROW_HEIGHT))
        title = label(cell, name, 9, 21, 145, bold=True)
        cell.setTextField_(title)
        badge = label(cell, state, 162, 22, 32, size=10, secondary=True)
        badge.setAlignment_(NSTextAlignmentRight)
        caption = label(cell, detail, 9, 3, 185, size=11, secondary=True)
        for text in (title, caption):
            text.cell().setLineBreakMode_(NSLineBreakByTruncatingTail)
        cell.setToolTip_(f"{name} · {state}\n{detail}")
        return cell

    def _rule_summary(self, rule: SourceAlertRule) -> str:
        """저장한 원본 이름과 변경 조건을 요약한다."""
        name = next((item.name for item in self._columns if item.id == rule.column_id), None)
        if name is None:
            return "연결할 칼럼을 다시 선택하세요"
        if rule.column_id == COLLECTION_STATE:
            return f"수집 실패 · {rule.target}" if rule.target else "수집 실패"
        if rule.member is not None:
            return f"{name} · {'내 계정' if rule.member == 'self' else '지정한 사람'} 추가"
        if rule.schedule is not None:
            if rule.schedule.mode == "before_start":
                return f"{name} · 시작 {rule.schedule.value}분 전"
            day = "전날" if rule.schedule.mode == "previous_day" else "당일"
            return f"{name} · {day} {rule.schedule.value}"
        if rule.target is None:
            return f"{name} · 값이 바뀔 때"
        if not rule.target:
            return f"{name} · 값이 비워질 때"
        return f"{name} → {rule.target}"

    def _refresh_list(self, *, reveal_selection: bool = False) -> None:
        """목록을 갱신하면서 프로그램 선택이 편집 콜백을 다시 부르지 않게 한다."""
        self._updating_list = True
        try:
            existing = self._index < self.rule_count
            self._editor_title.setStringValue_("규칙 편집" if existing else "새 규칙")
            self._new.setEnabled_(self.rule_count < MAX_SOURCE_ALERT_RULES)
            self._remove.setEnabled_(existing)
            self._empty.setHidden_(self.rule_count > 0)
            self.rule_list.setHidden_(not self.rule_count)
            self.rule_list.reloadData()
            row = self._index if existing else None
            if row is not None:
                self.rule_list.selectRowIndexes_byExtendingSelection_(
                    NSIndexSet.indexSetWithIndex_(row), False
                )
                if reveal_selection:
                    self.rule_list.scrollRowToVisible_(row)
            else:
                self.rule_list.deselectAll_(None)
        finally:
            self._updating_list = False

    def _popup(
        self, x: float, y: float, width: float, selector: str | None = None
    ) -> NSPopUpButton:
        """원본 속성 이름을 기본 macOS 목록으로 고른다."""
        control = NSPopUpButton.alloc().initWithFrame_pullsDown_(frame(x, y, width, 26), False)
        if selector is not None:
            control.setTarget_(self._actions)
            control.setAction_(selector)
        self.view.addSubview_(control)
        return control

    def _field(self, x: float, y: float, width: float, placeholder: str) -> NSTextField:
        """규칙 이름과 정확히 일치할 값을 입력한다."""
        control = NSTextField.textFieldWithString_("")
        control.setFrame_(frame(x, y, width, 24))
        control.setPlaceholderString_(placeholder)
        self.view.addSubview_(control)
        return control

    def _titles(self, control: NSPopUpButton, values: list[str]) -> None:
        """같은 이름도 별도 항목으로 유지해 선택 위치가 바뀌지 않게 한다."""
        control.removeAllItems()
        for value in values:
            item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(value, None, "")
            control.menu().addItem_(item)
        control.selectItemAtIndex_(0)

    def set_person_scope(self, scoped: bool) -> None:
        """값 변경을 적용할 항목 범위를 사용자가 확인할 수 있게 한다."""
        self._scope.setStringValue_(
            "현재 연결한 사람 속성으로 찾은 내 항목에 적용됩니다."
            if scoped
            else "이 소스에 있는 모든 항목에 적용됩니다."
        )

    def set_scope_description(self, description: str) -> None:
        """여러 소스에 적용하는 규칙은 카테고리의 범위를 안내한다."""
        self._scope.setStringValue_(description)

    def _chosen(self) -> CategoryColumn | None:
        """목록에 실제로 있는 원본 속성만 선택할 수 있다."""
        index = self.column.indexOfSelectedItem() - 1
        return self._columns[index] if 0 <= index < len(self._columns) else None

    def _fill(self, index: int) -> None:
        """저장한 값 또는 새 규칙을 정확한 원본 속성으로 복원한다."""
        self._index = index
        rule = self._rules[index] if index < len(self._rules) else None
        self._preview = None
        self.name.setStringValue_(rule.name if rule else "")
        self._titles(self.column, ["칼럼 선택", *(column.name for column in self._columns)])
        selected = next(
            (
                i
                for i, column in enumerate(self._columns, 1)
                if rule and column.id == rule.column_id
            ),
            0,
        )
        self.column.selectItemAtIndex_(selected)
        mode = 0 if rule is None else 1 if rule.target is None else 3 if rule.target == "" else 2
        self._load_conditions()
        if rule and rule.schedule:
            mode = next(
                (
                    i
                    for i, (key, _) in enumerate(self._schedule_choices, len(CHANGE_CONDITIONS))
                    if key == rule.schedule.mode
                ),
                0,
            )
        if rule and rule.member:
            mode = 4 if rule.member == "self" else 5
        self.condition.selectItemAtIndex_(mode)
        self._legacy_filter = rule.item_filter if rule else None
        self._scope_rules = rule.scope if rule else None
        self._refresh_scope()
        self.enabled.setState_(int(rule is not None and rule.enabled))
        self._fill_delivery(rule.delivery if rule else None, existing=rule is not None)
        self._load_target(
            rule.schedule.value
            if rule and rule.schedule
            else rule.member
            if rule and rule.member not in (None, "self")
            else rule.target
            if rule
            else None
        )
        self.render_condition()
        self.status.setStringValue_("")
        self._refresh_list(reveal_selection=True)

    def _fill_delivery(self, delivery: AlertDelivery | None, *, existing: bool) -> None:
        """새 규칙은 빈 입력으로 열고 이전 문서의 전달 방식만 별도로 표시한다."""
        titles = [title for title, _ in DELIVERY_CHOICES]
        index = 0
        if delivery is not None:
            index = next(
                i
                for i, (_, keys) in enumerate(DELIVERY_CHOICES)
                if set(keys) == set(delivery.channels)
            )
        elif existing:
            index = len(titles)
            titles.append("기존 문구와 채널 유지")
        self._titles(self.delivery_channel, titles)
        self.delivery_channel.selectItemAtIndex_(index)
        self.message.setStringValue_(delivery.message if delivery else "")

    def _read_delivery(self) -> AlertDelivery | None:
        """기존 동작 유지와 사용자가 명시적으로 비운 채널을 구분한다."""
        index = self.delivery_channel.indexOfSelectedItem()
        message = self.message.stringValue().strip()
        if index == len(DELIVERY_CHOICES):
            if message:
                raise SourceSettingsError("직접 입력한 문구를 받을 채널을 선택하세요.")
            return None
        try:
            return AlertDelivery(message, DELIVERY_CHOICES[index][1])
        except ValueError as error:
            raise SourceSettingsError(str(error)) from error

    def _load_conditions(self) -> None:
        """날짜·시간 칼럼에서만 원본 시작값을 사용하는 예약 조건을 제공한다."""
        column = self._chosen()
        self._schedule_choices = (
            SCHEDULE_CHOICES if column and column.kind in {"date", "time"} else ()
        )
        if column and column.id == COLLECTION_STATE:
            choices = ["조건 선택", "수집이 실패할 때", "지정한 오류가 발생할 때"]
        elif column and column.id in self._people_columns:
            choices = [*CHANGE_CONDITIONS, "내 계정이 추가될 때", "지정한 사람이 추가될 때"]
        else:
            choices = [*CHANGE_CONDITIONS, *(title for _, title in self._schedule_choices)]
        self._titles(self.condition, choices)
        self._scope_button.setEnabled_(column is not None and column.id != COLLECTION_STATE)

    def _refresh_scope(self) -> None:
        """규칙을 열 때 적용 중인 조건 수를 버튼에서 바로 보여 준다."""
        rules = self._scope_rules
        count = (len(rules.filters) + len(rules.exclusions) if rules else 0) + int(
            self._legacy_filter is not None
        )
        self._scope_button.setTitle_(
            f"적용 대상 · 조건 {count}개…" if count else "적용 대상 · 전체…"
        )

    def edit_scope(self) -> None:
        """확정한 조건만 현재 규칙에 반영하고 취소하면 직전 초안을 유지한다."""
        dialog = NSAlert.alloc().init()
        dialog.setMessageText_("알림 적용 대상")
        dialog.addButtonWithTitle_("적용")
        dialog.addButtonWithTitle_("취소")
        view = NSView.alloc().initWithFrame_(frame(0, 0, 642, 425))
        dialog.setAccessoryView_(view)
        editor = MainListEditor(
            view, self._columns[:-1], self._scope_rules, self._clear_sheet_status, alert_scope=True
        )
        legacy = None
        if self._legacy_filter:
            # 이전 규칙의 특수 범위는 사용자가 직접 해제할 때까지 함께 적용한다.
            title = (
                "기존 범위 유지 · 회의로 분류된 항목"
                if self._legacy_filter == "meeting"
                else "기존 범위 유지 · 지나지 않은 일정"
            )
            legacy = NSButton.checkboxWithTitle_target_action_(title, self._actions, "target:")
            legacy.setFrame_(frame(8, 395, 450, 26))
            legacy.setState_(1)
            view.addSubview_(legacy)
        while dialog.runModal() == NSAlertFirstButtonReturn:
            try:
                self._scope_rules = editor.draft()
            except ValueError as error:
                dialog.setInformativeText_(str(error))
                continue
            if legacy is not None and legacy.state() != 1:
                self._legacy_filter = None
            self._refresh_scope()
            self.input_changed()
            return

    def _schedule_mode(self) -> ScheduleMode | None:
        """값 변경 조건과 같은 목록에서 사용자가 선택한 예약 방식을 확인한다."""
        index = self.condition.indexOfSelectedItem() - len(CHANGE_CONDITIONS)
        return (
            self._schedule_choices[index][0] if 0 <= index < len(self._schedule_choices) else None
        )

    def _load_target(self, value: str | None) -> None:
        """참·거짓 칼럼만 선택지를 제공하고 다른 값은 직접 입력한다."""
        column = self._chosen()
        self._visible_column = column
        options: tuple[str, ...] = ("참", "거짓") if column and column.kind == "boolean" else ()
        if column and column.id == COLLECTION_STATE:
            options = tuple(sorted(COLLECTION_FAILURE_CODES))
        self._target_values = options
        self.target.setStringValue_(value or "")
        self.target.setPlaceholderString_(
            "예: 09:30" if column and column.kind == "time" else "알림을 받을 값 입력"
        )
        choices = ["값 선택", *options]
        if options and value and value not in options:
            self._target_values = (*self._target_values, value)
            choices.append(value)
        self._titles(self._target_choice, choices)
        if self._target_values and value in self._target_values:
            self._target_choice.selectItemAtIndex_(self._target_values.index(value) + 1)

    def change_column(self) -> None:
        """속성을 바꾸면 이전 선택형 값을 새 조건에 실수로 쓰지 않는다."""
        if self._visible_column != self._chosen():
            self._load_conditions()
            column = self._chosen()
            if column is None or column.id == COLLECTION_STATE:
                self._scope_rules = None
                self._legacy_filter = None
                self._refresh_scope()
            self._load_target(None)
        self.input_changed()
        self.render_condition()

    def input_changed(self) -> None:
        """현재 입력의 오류 상태만 지운다."""
        self.status.setStringValue_("")
        self._clear_sheet_status()
        if self._index < self.rule_count:
            current = self._rules[self._index]
            try:
                preview = self._read_rule(current.id)
            except SourceSettingsError:
                preview = None
            if preview != self._preview:
                self._preview = preview
                self._refresh_list()

    def render_condition(self) -> None:
        """모든 변경을 알리는 조건이면 값 선택을 접는다."""
        column = self._chosen()
        person = bool(
            column
            and column.id in self._people_columns
            and self.condition.indexOfSelectedItem() == 5
        )
        failure = bool(column and column.id == COLLECTION_STATE)
        specific = self.condition.indexOfSelectedItem() == 2 or person
        mode = self._schedule_mode()
        self._target_label.setStringValue_(
            "사람 ID"
            if person
            else "오류 코드"
            if failure
            else "몇 분 전"
            if mode == "before_start"
            else "알림 시각"
            if mode
            else "변경 후 값"
        )
        self.target.setPlaceholderString_(
            "Notion 사용자 ID"
            if person
            else "오류 코드 입력"
            if failure
            else "분 입력"
            if mode == "before_start"
            else "HH:MM · 서울 시간"
            if mode
            else "알림을 받을 값 입력"
        )
        self._target_label.setHidden_(not specific and mode is None)
        self.target.setHidden_(mode is None and (not specific or bool(self._target_values)))
        self._target_choice.setHidden_(not specific or not self._target_values)

    def _read_rule(self, identifier: str) -> SourceAlertRule:
        """입력값으로 검증한 규칙을 구성하고 목록을 건드리지 않는다."""
        name, column = self.name.stringValue().strip(), self._chosen()
        if not name or len(name) > 80 or any(ord(char) < 32 for char in name):
            raise SourceSettingsError("알림 이름을 1~80자로 입력하세요.")
        existing = self._rules[self._index] if self._index < self.rule_count else None
        if column is None:
            if existing and existing.column_id == UNBOUND_COLUMN and self.enabled.state() != 1:
                return replace(existing, name=name, delivery=self._read_delivery())
            raise SourceSettingsError("규칙에 사용할 기준 데이터를 선택하세요.")
        mode = self.condition.indexOfSelectedItem()
        if mode == 0:
            raise SourceSettingsError("알림을 보낼 조건을 선택하세요.")
        target = "" if mode == 3 else None
        if mode == 2:
            index = self._target_choice.indexOfSelectedItem() - 1
            if self._target_values and index < 0:
                raise SourceSettingsError("변경 후 값을 선택하세요.")
            target = (
                self._target_values[index]
                if self._target_values
                else " ".join(self.target.stringValue().split())
            )
            if not target:
                raise SourceSettingsError("변경 후 값을 입력하세요.")
            if len(target) > 240:
                raise SourceSettingsError("변경 후 값은 240자 이내로 입력하세요.")
        schedule_mode = self._schedule_mode()
        try:
            schedule = (
                AlertSchedule(schedule_mode, self.target.stringValue().strip())
                if schedule_mode
                else None
            )
        except ValueError as error:
            raise SourceSettingsError(str(error)) from error
        member = None
        if column.id in self._people_columns and mode in (4, 5):
            try:
                member = "self" if mode == 4 else str(UUID(self.target.stringValue().strip()))
            except ValueError as error:
                raise SourceSettingsError("Notion 사용자 ID를 입력하세요.") from error
        proposed = SourceAlertRule(
            identifier,
            name,
            column.id,
            column.kind,
            target,
            self.enabled.state() == 1,
            delivery=self._read_delivery(),
            schedule=schedule,
            member=member,
            item_filter=self._legacy_filter,
            scope=self._scope_rules,
        )
        if (
            existing
            and replace(
                existing,
                previous_kind=None,
                name=name,
                delivery=proposed.delivery,
                enabled=proposed.enabled,
            )
            == proposed
        ):
            return replace(proposed, previous_kind=existing.previous_kind)
        return proposed

    def _commit(self) -> None:
        """현재 행이 비어 있으면 그대로 두고 작성한 조건만 반영한다."""
        existing = self._rules[self._index] if self._index < len(self._rules) else None
        if (
            existing is None
            and not self.name.stringValue().strip()
            and self._chosen() is None
            and not self.message.stringValue().strip()
            and self.delivery_channel.indexOfSelectedItem() == 0
            and self.condition.indexOfSelectedItem() == 0
            and self.enabled.state() != 1
        ):
            return
        rule = self._read_rule(existing.id if existing else str(uuid4()))
        if existing is not None:
            self._rules[self._index] = rule
        elif len(self._rules) < MAX_SOURCE_ALERT_RULES:
            self._rules.append(rule)
        else:
            raise SourceSettingsError("소스마다 규칙을 24개까지 저장할 수 있습니다.")

    def _commit_before_navigation(self) -> bool:
        """현재 규칙이 완성되지 않았으면 이름과 입력을 보존한다."""
        try:
            self._commit()
        except SourceSettingsError as error:
            self._refresh_list()
            self.status.setStringValue_(str(error))
            return False
        return True

    def select_rule(self, index: int) -> None:
        """선택한 사용자 규칙을 열고 이전 입력을 유지한다."""
        if (
            not self._updating_list
            and 0 <= index < self.rule_count
            and index != self._index
            and self._commit_before_navigation()
        ):
            self._fill(index)

    def open_new(self) -> None:
        """새 규칙은 기준 데이터와 조건을 비운 상태에서 시작한다."""
        if self._commit_before_navigation():
            self._fill(self.rule_count)

    def remove(self) -> None:
        """선택한 사용자 규칙만 삭제한다."""
        if self._index < self.rule_count:
            self._rules.pop(self._index)
            self._fill(min(self._index, self.rule_count))

    def draft(self) -> tuple[SourceAlertRule, ...]:
        """완성된 규칙과 현재 입력을 시트 적용 결과에 포함한다."""
        previous = tuple(self._rules)
        self._commit()
        if previous != tuple(self._rules):
            self._preview = None
            self._refresh_list()
        return tuple(self._rules)
