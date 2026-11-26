"""수집 소스 한 곳의 분류 규칙과 우선순위를 편집한다."""

from collections.abc import Callable
from dataclasses import dataclass
from weakref import proxy

from AppKit import NSButton, NSComboBox, NSPopUpButton, NSScrollView, NSTextField, NSView
from Foundation import NSObject

from src.application.category_column_types import accepts_column_type
from src.application.source_settings import SourceColumn, SourceSettingsError
from src.application.source_settings_codec import parse_classification_rules
from src.domain.models import ClassificationCondition, ClassificationRule
from src.presentation.category_sheet_controls import (
    field,
    observed_field,
    popup,
    set_observed_choices,
)
from src.presentation.native_views import button, frame, label


@dataclass
class ConditionInput:
    """스키마에서 사라진 칼럼도 편집하지 않으면 기존 연결을 유지한다."""

    picker: NSPopUpButton
    value: NSComboBox
    choices: list[tuple[str, str]]

    def read(self) -> ClassificationCondition:
        """선택한 원본과 공백일 수도 있는 비교값을 가져온다."""
        prop, kind = self.choices[self.picker.indexOfSelectedItem()]
        return ClassificationCondition(prop, kind, self.value.stringValue())


@dataclass
class RuleInput:
    """한 규칙에 속한 편집 컨트롤을 함께 읽는다."""

    mode: NSPopUpButton
    result: NSTextField
    conditions: list[ConditionInput]

    def read(self) -> ClassificationRule:
        """재배치 전 아직 확정하지 않은 입력까지 읽는다."""
        return ClassificationRule(
            "any" if self.mode.indexOfSelectedItem() else "all",
            tuple(c.read() for c in self.conditions),
            self.result.stringValue(),
        )


class ClassificationActions(NSObject):
    """시트의 버튼 조작을 편집기로 전달한다."""

    owner: "SourceClassificationEditor"

    def change_(self, sender: NSButton) -> None:
        """버튼에 등록된 행 변경을 수행한다."""
        self.owner.change(sender.tag())

    def column_(self, sender: NSPopUpButton) -> None:
        """바꾼 조건 칼럼에 맞는 선택 후보를 제공한다."""
        self.owner.update_choices(sender)

    def select_(self, sender: object) -> None:
        """결과를 표시할 칼럼을 선택하면 규칙 편집을 활성화한다."""
        self.owner.refresh()


class SourceClassificationEditor:
    """분류 결과를 기록할 칼럼과 순서 있는 비교 조건을 관리한다."""

    def __init__(
        self,
        page: NSView,
        listing: NSScrollView,
        columns: tuple[SourceColumn, ...],
        saved: dict[str, object],
        same: Callable[[str, str | None], bool],
    ) -> None:
        """기존 설정을 편집 초안으로 읽고 누락된 연결을 보존한다."""
        self._columns = [c for c in columns if accepts_column_type("notion", "text", c.kind)]
        self._same = same
        self._saved = saved
        self._actions = ClassificationActions.alloc().init()
        self._actions.owner = proxy(self)
        self._list = listing
        self._inputs: list[RuleInput] = []
        self._commands: list[tuple[str, int, int]] = []
        label(page, "결과 표시 칼럼", 10, 307, 125)
        self._picker = popup(page, 140, 302, 482, ["사용 안 함", *(c.name for c in self._columns)])
        prop = str(saved.get("property", ""))
        index = next((i for i, c in enumerate(self._columns, 1) if same(c.id, prop)), 0)
        if prop and not index:
            self._picker.addItemsWithTitles_([str(saved["name"]) + " (기존 연결)"])
            index = len(self._columns) + 1
        self._picker.selectItemAtIndex_(index)
        self._picker.setTarget_(self._actions)
        self._picker.setAction_("select:")
        label(page, "위에서부터 처음 일치한 규칙을 적용합니다.", 10, 262, 430)
        self._add = button(page, "+ 규칙 추가", 503, 255, 119, self._actions, "change:")
        self._add.setTag_(-1)
        label(page, "그 밖의 값", 10, 9, 102)
        self._default = field(
            page, str(saved.get("default", "")), 119, 5, 503, "결과 칼럼의 원본 값 유지"
        )
        rules = list(parse_classification_rules(saved.get("rules", []), "notion"))
        pairs = saved.get("values")
        if isinstance(pairs, dict):
            rules.extend(
                ClassificationRule(
                    "all", (ClassificationCondition(prop, str(saved["kind"]), k),), v
                )
                for k, v in pairs.items()
            )
        self.render(rules)
        if self._inputs:
            first = self._inputs[0].mode
            first.scrollRectToVisible_(first.bounds())
        self.refresh()

    def refresh(self) -> None:
        """비활성화한 연결의 초안은 시트 안에서 유지한다."""
        active = self._picker.indexOfSelectedItem() > 0
        self._add.setEnabled_(active and bool(self._columns))
        self._default.setEnabled_(active)
        self._list.setHidden_(not active)

    def action(
        self,
        body: NSView,
        title: str,
        x: int,
        y: int,
        width: int,
        op: str,
        row: int,
        condition: int = 0,
    ) -> NSButton:
        """삭제와 순서 변경의 대상 행을 버튼에 연결한다."""
        control = button(body, title, x, y, width, self._actions, "change:")
        control.setTag_(len(self._commands))
        self._commands.append((op, row, condition))
        return control

    def render(self, rules: list[ClassificationRule]) -> None:
        """각 규칙의 결합 방식과 조건을 같은 묶음으로 표시한다."""
        height = max(210, sum(48 + 34 * len(r.conditions) for r in rules) + 8)
        body = NSView.alloc().initWithFrame_(frame(0, 0, 622, height))
        self._inputs.clear()
        self._commands.clear()
        y = height - 36
        for i, rule in enumerate(rules):
            mode = popup(body, 5, y, 132, ["모두 일치", "하나라도 일치"])
            mode.selectItemAtIndex_(int(rule.match == "any"))
            result = field(body, rule.result, 142, y, 170, "분류 이름")
            self.action(body, "↑", 314, y, 38, "up", i).setEnabled_(i > 0)
            self.action(body, "↓", 352, y, 38, "down", i).setEnabled_(i + 1 < len(rules))
            self.action(body, "+ 조건", 392, y, 99, "condition", i).setEnabled_(bool(self._columns))
            self.action(body, "규칙 삭제", 495, y, 118, "remove", i)
            inputs = []
            for j, c in enumerate(rule.conditions):
                y -= 34
                choices = [(col.id, col.kind) for col in self._columns]
                names = [col.name for col in self._columns]
                index = next(
                    (
                        n
                        for n, col in enumerate(self._columns)
                        if self._same(col.id, c.property_id)
                        and (
                            col.kind == c.kind
                            or c.kind == "status_or_select"
                            and col.kind in ("status", "select")
                        )
                    ),
                    len(choices),
                )
                if index == len(choices):
                    choices.append((c.property_id, c.kind))
                    names.append(c.property_id + " (기존 연결)")
                else:
                    choices[index] = (c.property_id, c.kind)
                picker = popup(body, 22, y, 202, names)
                picker.selectItemAtIndex_(index)
                picker.setTarget_(self._actions)
                picker.setAction_("column:")
                options = self._columns[index].options if index < len(self._columns) else ()
                value = observed_field(body, c.value, 229, y, 255, options)
                value.setPlaceholderString_("값 선택·직접 입력 (비우면 빈 값)")
                inputs.append(ConditionInput(picker, value, choices))
                self.action(body, "조건 삭제", 495, y, 118, "delete", i, j).setEnabled_(
                    len(rule.conditions) > 1
                )
            self._inputs.append(RuleInput(mode, result, inputs))
            y -= 48
        self._list.setDocumentView_(body)

    def update_choices(self, picker: NSPopUpButton) -> None:
        """입력 중인 비교값은 유지하고 선택 가능한 후보만 교체한다."""
        index = picker.indexOfSelectedItem()
        for row in self._inputs:
            for condition in row.conditions:
                if condition.picker == picker:
                    choices = self._columns[index].options if index < len(self._columns) else ()
                    set_observed_choices(condition.value, choices)
                    return

    def change(self, tag: int) -> None:
        """화면의 입력을 보존한 뒤 지정한 규칙이나 조건만 변경한다."""
        rules = [row.read() for row in self._inputs]
        op, i, j = self._commands[tag] if tag >= 0 else ("add", len(rules), 0)
        if op in ("add", "condition"):
            selected = self._picker.indexOfSelectedItem() - 1
            col = (
                self._columns[selected] if 0 <= selected < len(self._columns) else self._columns[0]
            )
            condition = ClassificationCondition(col.id, col.kind, "")
            if op == "add":
                rules.append(ClassificationRule("all", (condition,), ""))
            else:
                rule = rules[i]
                rules[i] = ClassificationRule(
                    rule.match, (*rule.conditions, condition), rule.result
                )
        elif op == "delete":
            rule = rules[i]
            rules[i] = ClassificationRule(
                rule.match, rule.conditions[:j] + rule.conditions[j + 1 :], rule.result
            )
        elif op == "remove":
            rules.pop(i)
        else:
            dest = i - 1 if op == "up" else i + 1
            rules.insert(dest, rules.pop(i))
        self.render(rules)
        if rules:
            target = min(i if op not in ("up", "down") else dest, len(rules) - 1)
            self._inputs[target].result.scrollRectToVisible_(self._inputs[target].result.bounds())

    def draft(self) -> dict[str, object]:
        """분류 우선순위와 결과 칼럼의 기존 저장 키를 보존한다."""
        index = self._picker.indexOfSelectedItem()
        if not index:
            return {}
        data = dict(self._saved)
        if index <= len(self._columns):
            col = self._columns[index - 1]
            if not self._same(col.id, str(data.get("property", ""))):
                data = {}
            data.update(property=data.get("property", col.id), kind=col.kind, name=col.name)
        data.pop("values", None)
        rules = [row.read() for row in self._inputs]
        if any(not rule.result.strip() for rule in rules):
            raise SourceSettingsError("규칙마다 분류 이름을 입력하세요.")
        data["rules"] = [
            {
                "match": r.match,
                "result": r.result.strip(),
                "conditions": [
                    {"property": c.property_id, "kind": c.kind, "value": c.value}
                    for c in r.conditions
                ],
            }
            for r in rules
        ]
        data.pop("default", None)
        if fallback := self._default.stringValue().strip():
            data["default"] = fallback
        return data
