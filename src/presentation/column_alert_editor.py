"""카테고리별 알림 목록에서 개별 규칙 편집 시트를 연다."""

from collections.abc import Callable
from weakref import proxy

from AppKit import NSButton, NSLineBreakByTruncatingTail, NSView
from Foundation import NSObject

from src.domain.category_configuration import CategoryColumn
from src.domain.source_alerts import COLLECTION_STATE, MAX_SOURCE_ALERT_RULES, SourceAlertRule
from src.presentation.alert_rule_sheet import AlertRuleSheet
from src.presentation.column_setup_sheet import scroll
from src.presentation.native_views import button, card, frame, label


class AlertListActions(NSObject):
    """규칙 목록의 추가·편집·삭제를 선택한 행에 연결한다."""

    owner: "ColumnAlertEditor"

    def newRule_(self, sender: object) -> None:
        """빈 규칙 편집기를 연다."""
        self.owner.edit(None)

    def editRule_(self, sender: NSButton) -> None:
        """누른 행의 규칙 전체를 연다."""
        self.owner.edit(sender.tag())

    def removeRule_(self, sender: NSButton) -> None:
        """해당 규칙을 카테고리 초안에서 지운다."""
        self.owner.remove(sender.tag())


class ColumnAlertEditor:
    """카테고리에 적용할 규칙 목록과 현재 열린 시트를 관리한다."""

    def __init__(
        self,
        parent: NSView,
        columns: tuple[CategoryColumn, ...],
        rules: tuple[SourceAlertRule, ...],
        clear_sheet_status: Callable[[], None],
        *,
        people_columns: frozenset[str] = frozenset(),
    ) -> None:
        """카테고리를 저장하기 전까지 확정한 규칙들을 초안으로 보관한다."""
        self.view = parent
        self._columns = columns
        self._people_columns = (
            people_columns
            | {col.id for col in columns if col.legacy_kind == "people"}
            | {rule.column_id for rule in rules if rule.member}
        )
        self._rules = list(rules)
        self._clear_status = clear_sheet_status
        self._sheet: AlertRuleSheet | None = None
        self._actions = AlertListActions.alloc().init()
        self._actions.owner = proxy(self)
        label(parent, "알림 규칙", 10, 344, 320, size=14, bold=True)
        self._new = button(parent, "+ 새 규칙", 510, 340, 118, self._actions, "newRule:")
        self._list = scroll(parent, 0, 0, 642, 326)
        self._render()

    def _render(self, reveal: int | None = None) -> None:
        """규칙 이름·발송 조건·사용 여부와 편집 버튼을 한 행에 표시한다."""
        height = max(326, len(self._rules) * 60)
        old = self._list.documentView()
        offset = old.frame()[1][1] - 326 - self._list.contentView().bounds()[0][1] if old else 0
        view = NSView.alloc().initWithFrame_(frame(0, 0, 626, height))
        if not self._rules:
            label(view, "등록된 규칙이 없습니다.", 12, height - 38, 590, secondary=True)
        self._new.setEnabled_(len(self._rules) < MAX_SOURCE_ALERT_RULES)
        for index, rule in enumerate(self._rules):
            y = height - (index + 1) * 60
            card(view, 0, y + 3, 625, 54)
            title = label(view, rule.name, 12, y + 30, 385, bold=True)
            caption = label(view, self._rule_summary(rule), 12, y + 9, 385, secondary=True)
            for text in (title, caption):
                text.cell().setLineBreakMode_(NSLineBreakByTruncatingTail)
                text.setToolTip_(text.stringValue())
            label(view, "켜짐" if rule.enabled else "꺼짐", 402, y + 21, 40, secondary=True)
            for text_label, x, action in (
                ("편집…", 452, "editRule:"),
                ("삭제", 540, "removeRule:"),
            ):
                control = button(view, text_label, x, y + 16, 76, self._actions, action)
                control.setTag_(index)
                control.setAccessibilityLabel_(f"{rule.name} {text_label}")
        self._list.setDocumentView_(view)
        origin = max(0, min(height - 326, height - 326 - offset))
        if reveal is not None:
            bottom = height - (reveal + 1) * 60
            origin = max(0, min(bottom, max(origin, bottom + 60 - 326)))
        view.scrollPoint_((0, origin))

    def _rule_summary(self, rule: SourceAlertRule) -> str:
        """저장한 원본 이름과 변경 조건을 요약한다."""
        if rule.column_id == COLLECTION_STATE:
            return f"수집 실패 · {rule.target}" if rule.target else "수집 실패"
        name = next((item.name for item in self._columns if item.id == rule.column_id), None)
        if name is None:
            return "연결할 칼럼을 다시 선택하세요"
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

    def edit(self, index: int | None) -> None:
        """적용한 결과만 목록에 추가하거나 원래 위치에 교체한다."""
        parent = self.view.window()
        if parent is None or self._sheet is not None:
            return
        if index is None and len(self._rules) >= MAX_SOURCE_ALERT_RULES:
            return

        def completed(rule: SourceAlertRule | None) -> None:
            """취소하면 목록과 스크롤 위치를 그대로 둔다."""
            self._sheet = None
            if rule is None:
                return
            if index is None:
                self._rules.append(rule)
            else:
                self._rules[index] = rule
            self._clear_status()
            self._render(len(self._rules) - 1 if index is None else index)

        self._sheet = AlertRuleSheet(
            parent,
            self._columns,
            self._rules[index] if index is not None else None,
            self._people_columns,
            completed,
        )
        self._sheet.show()

    def remove(self, index: int) -> None:
        """선택한 규칙을 카테고리 저장 전 목록에서 제거한다."""
        self._rules.pop(index)
        self._clear_status()
        self._render()

    def draft(self) -> tuple[SourceAlertRule, ...]:
        """규칙 시트에서 적용한 결과만 카테고리 저장에 사용한다."""
        return tuple(self._rules)
