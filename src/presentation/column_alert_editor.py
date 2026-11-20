"""카테고리에 정의한 저장 칼럼과 직접 입력한 조건으로 알림을 작성한다."""

from collections.abc import Callable, Mapping
from uuid import uuid4
from weakref import proxy

from AppKit import (
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

from src.application.source_settings import SourceSettingsError
from src.domain.category_configuration import CategoryColumn
from src.domain.category_event_alerts import (
    EVENT_ALERT_TRIGGERS,
    CategoryEventAlert,
)
from src.domain.source_alerts import MAX_SOURCE_ALERT_RULES, SourceAlertRule
from src.presentation.native_views import button, card, frame, label

RULE_ROW_HEIGHT = 42.0
RULE_LIST_WIDTH = 226.0


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
        return self.owner.listed_count

    def tableView_viewForTableColumn_row_(
        self, table: NSTableView, column: NSTableColumn | None, row: int
    ) -> NSView | None:
        """규칙 이름과 조건을 한 행에서 읽을 수 있도록 구성한다."""
        return self.owner.rule_row(row)

    def tableViewSelectionDidChange_(self, notification: object) -> None:
        """클릭과 방향키 선택을 같은 편집기에 반영한다."""
        self.owner.select_rule(self.owner.rule_list.selectedRow())

    def newRule_(self, sender: object) -> None:
        """카테고리에 정의한 칼럼으로 새 규칙을 작성한다."""
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
        events: tuple[CategoryEventAlert, ...] = (),
        event_states: Mapping[str, bool] | None = None,
        event_changes: Mapping[str, bool] | None = None,
    ) -> None:
        """시트를 적용할 때까지 규칙 편집을 외부에 저장하지 않는다."""
        self._columns = columns
        self._rules = list(rules)
        self._events = list(events)
        self._event_states = dict(event_states or {})
        self._event_changes = dict(event_changes or {})
        self._event_index: int | None = None
        self._index = 0
        self._preview: SourceAlertRule | None = None
        self._updating_list = False
        self._clear_sheet_status = clear_sheet_status
        self._actions = AlertEditorActions.alloc().init()
        self._actions.owner = proxy(self)
        self.view = parent
        self._scope = label(parent, "", 10, 348, 620, secondary=True)
        card(parent, 0, 41, 242, 290)
        label(parent, "알림 규칙", 14, 294, 105, bold=True)
        self._new = button(parent, "+ 새 규칙", 126, 290, 104, self._actions, "newRule:")
        self._empty = label(parent, "등록된 규칙이 없습니다.", 14, 249, 214, secondary=True)
        self.rule_list = self._make_list(parent)
        card(parent, 254, 41, 388, 290)
        self._editor_title = label(parent, "", 268, 294, 360, bold=True)
        label(parent, "알림 이름", 268, 248, 86)
        self.name = self._field(359, 244, 269, "알림에 표시할 이름")
        column_label = label(parent, "알림 받을 칼럼", 268, 211, 86)
        self.column = self._popup(359, 207, 269, "column:")
        self.condition = self._popup(359, 170, 269, "condition:")
        self._titles(
            self.condition, ["조건 선택", "값이 바뀔 때", "특정 값으로 바뀔 때", "값이 비워질 때"]
        )
        condition_label = label(parent, "언제 보낼지", 268, 174, 86)
        self._target_label = label(parent, "변경 후 값", 268, 138, 86)
        self.target = self._field(359, 134, 269, "원본의 값과 같게 입력")
        self._target_choice = self._popup(359, 131, 269, "target:")
        self._target_values: tuple[str, ...] = ()
        self._visible_column: CategoryColumn | None = None
        self.enabled = NSButton.checkboxWithTitle_target_action_(
            "이 규칙 사용", self._actions, "condition:"
        )
        self.enabled.setFrame_(frame(268, 85, 183, 26))
        parent.addSubview_(self.enabled)
        for field in (self.name, self.target):
            field.setDelegate_(self._actions)
        self._remove = button(parent, "규칙 삭제", 522, 84, 106, self._actions, "removeRule:")
        self._edit_controls: tuple[NSView, ...] = (
            column_label,
            self.column,
            condition_label,
            self.condition,
        )
        self._event_label = label(parent, "언제 보낼지", 268, 211, 86)
        self.event_condition = label(parent, "", 359, 211, 269)
        self.status = label(parent, "", 14, 14, 610, secondary=True)
        self.rule_list.setDataSource_(self._actions)
        self.rule_list.setDelegate_(self._actions)
        self.set_person_scope(person_scoped)
        if events:
            self._fill_event(0)
        else:
            self._fill(0)

    @property
    def rule_count(self) -> int:
        """작성 중인 빈 입력은 규칙 수에 넣지 않는다."""
        return len(self._rules)

    @property
    def listed_count(self) -> int:
        """이벤트와 칼럼 조건을 한 목록에 표시한다."""
        return len(self._events) + self.rule_count

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
        if not 0 <= index < self.listed_count:
            return None
        if index < len(self._events):
            event = self._events[index]
            name = (
                (self.name.stringValue().strip() or event.name)
                if index == self._event_index
                else event.name
            )
            detail = EVENT_ALERT_TRIGGERS[event.trigger].condition
            enabled = (
                self.enabled.state() == 1
                if index == self._event_index
                else self.event_enabled(event.trigger)
            )
            state = "켜짐" if enabled else "꺼짐"
        else:
            position = index - len(self._events)
            rule = (
                self._preview
                if self._preview is not None and position == self._index
                else self._rules[position]
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
        if rule.target is None:
            return f"{name} · 값이 바뀔 때"
        if not rule.target:
            return f"{name} · 값이 비워질 때"
        return f"{name} → {rule.target}"

    def _refresh_list(self, *, reveal_selection: bool = False) -> None:
        """목록을 갱신하면서 프로그램 선택이 편집 콜백을 다시 부르지 않게 한다."""
        self._updating_list = True
        try:
            position = self._event_index
            existing = (
                position < len(self._events)
                if position is not None
                else self._index < self.rule_count
            )
            self._editor_title.setStringValue_("규칙 편집" if existing else "새 규칙")
            self._new.setEnabled_(self.rule_count < MAX_SOURCE_ALERT_RULES)
            self._remove.setEnabled_(existing)
            self._empty.setHidden_(self.listed_count > 0)
            self.rule_list.setHidden_(self.listed_count == 0)
            self.rule_list.reloadData()
            row = (
                (position if position is not None else len(self._events) + self._index)
                if existing
                else None
            )
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

    def event_enabled(self, trigger: str) -> bool:
        """외부 알림 설정 위에 이 시트에서 바꾼 선택을 덮어쓴다."""
        return self._event_changes.get(trigger, self._event_states.get(trigger, False))

    def update_event_states(self, states: Mapping[str, bool]) -> None:
        """알림 설정이 읽혀도 시트에서 직접 바꾼 값은 보존한다."""
        if states == self._event_states:
            return
        selected = self._chosen_event() if self._event_index is not None else None
        unchanged = selected is not None and (self.enabled.state() == 1) == self.event_enabled(
            selected
        )
        self._event_states = dict(states)
        if unchanged and selected is not None:
            self.enabled.setState_(int(self.event_enabled(selected)))
        self._refresh_list()

    def _chosen_event(self) -> str | None:
        """이전 문서에 명시한 이벤트만 편집할 수 있다."""
        return self._events[self._event_index].trigger if self._event_index is not None else None

    def _fill_event(self, index: int) -> None:
        """기존 이벤트의 이름과 사용 여부를 복원하고 원래 조건을 표시한다."""
        self._event_index = index
        self._index = self.rule_count
        self._preview = None
        rule = self._events[index]
        self.name.setStringValue_(rule.name)
        self.event_condition.setStringValue_(EVENT_ALERT_TRIGGERS[rule.trigger].condition)
        self.enabled.setState_(int(self.event_enabled(rule.trigger)))
        self._present_event(True)
        self.render_condition()
        self.status.setStringValue_("")
        self._refresh_list(reveal_selection=True)

    def _present_event(self, show: bool) -> None:
        """조건 종류에 맞는 입력만 보여 준다."""
        for control in self._edit_controls:
            control.setHidden_(show)
        self._event_label.setHidden_(not show)
        self.event_condition.setHidden_(not show)

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
        self._event_index = None
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
        self.condition.selectItemAtIndex_(mode)
        self.enabled.setState_(int(rule is not None and rule.enabled))
        self._load_target(rule.target if rule else None)
        self._present_event(False)
        self.render_condition()
        self.status.setStringValue_("")
        self._refresh_list(reveal_selection=True)

    def _load_target(self, value: str | None) -> None:
        """참·거짓 칼럼만 선택지를 제공하고 다른 값은 직접 입력한다."""
        column = self._chosen()
        self._visible_column = column
        options = ("참", "거짓") if column and column.kind == "boolean" else ()
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
            self._load_target(None)
        self.input_changed()
        self.render_condition()

    def input_changed(self) -> None:
        """현재 입력의 오류 상태만 지운다."""
        self.status.setStringValue_("")
        self._clear_sheet_status()
        if self._event_index is not None:
            self._refresh_list()
        elif self._index < self.rule_count:
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
        specific = self._event_index is None and self.condition.indexOfSelectedItem() == 2
        self._target_label.setHidden_(not specific)
        self.target.setHidden_(not specific or bool(self._target_values))
        self._target_choice.setHidden_(not specific or not self._target_values)

    def _read_rule(self, identifier: str) -> SourceAlertRule:
        """입력값으로 검증한 규칙을 구성하고 목록을 건드리지 않는다."""
        name, column = self.name.stringValue().strip(), self._chosen()
        if not name or len(name) > 80 or any(ord(char) < 32 for char in name):
            raise SourceSettingsError("알림 이름을 1~80자로 입력하세요.")
        if column is None:
            raise SourceSettingsError("값이 바뀌면 알림을 받을 칼럼을 선택하세요.")
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
        return SourceAlertRule(
            identifier,
            name,
            column.id,
            column.kind,
            target,
            self.enabled.state() == 1,
        )

    def _commit(self) -> None:
        """현재 행이 비어 있으면 그대로 두고 작성한 조건만 반영한다."""
        if self._event_index is not None:
            self._commit_event(self._event_index)
            return
        existing = self._rules[self._index] if self._index < len(self._rules) else None
        if existing is None and not self.name.stringValue().strip() and self._chosen() is None:
            return
        rule = self._read_rule(existing.id if existing else str(uuid4()))
        if existing is not None:
            self._rules[self._index] = rule
        elif len(self._rules) < MAX_SOURCE_ALERT_RULES:
            self._rules.append(rule)
        else:
            raise SourceSettingsError("소스마다 규칙을 24개까지 저장할 수 있습니다.")

    def _commit_event(self, index: int) -> None:
        """이벤트 이름과 조건이 유효하면 카테고리 사용자 규칙으로 반영한다."""
        existing = self._events[index]
        name = self.name.stringValue().strip()
        if not name or len(name) > 80 or any(ord(char) < 32 for char in name):
            raise SourceSettingsError("알림 이름을 1~80자로 입력하세요.")
        enabled = self.enabled.state() == 1
        if enabled != self.event_enabled(existing.trigger):
            self._event_changes[existing.trigger] = enabled
        self._events[index] = CategoryEventAlert(existing.trigger, name)

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
        if self._updating_list or not 0 <= index < self.listed_count:
            return
        if index < len(self._events):
            if index != self._event_index and self._commit_before_navigation():
                self._fill_event(index)
            return
        position = index - len(self._events)
        if (
            self._event_index is not None or position != self._index
        ) and self._commit_before_navigation():
            self._fill(position)

    def open_new(self) -> None:
        """내용을 보존하고 사용자가 선택한 종류의 규칙을 만든다."""
        if self._commit_before_navigation():
            self._fill(self.rule_count)

    def remove(self) -> None:
        """선택한 조건의 규칙만 초안에서 지운다."""
        index = self._event_index
        if index is not None and index < len(self._events):
            self._events.pop(index)
            if self._events:
                self._fill_event(min(index, len(self._events) - 1))
            else:
                self._fill(0)
        elif index is None and self._index < self.rule_count:
            self._rules.pop(self._index)
            if self._events and not self._rules:
                self._fill_event(0)
            else:
                self._fill(min(self._index, self.rule_count))

    def draft(self) -> tuple[SourceAlertRule, ...]:
        """완성된 규칙과 현재 입력을 시트 적용 결과에 포함한다."""
        previous = (tuple(self._rules), tuple(self._events))
        self._commit()
        if previous != (tuple(self._rules), tuple(self._events)):
            self._preview = None
            self._refresh_list()
        return tuple(self._rules)

    def event_draft(self) -> tuple[tuple[CategoryEventAlert, ...], dict[str, bool]]:
        """등록된 이벤트와 현재 시트에서 변경한 스위치를 반환한다."""
        active = {item.trigger for item in self._events}
        return tuple(self._events), {
            key: value for key, value in self._event_changes.items() if key in active
        }
