"""항목 범위와 발송 조건을 함께 편집하는 알림 규칙 시트다."""

from collections.abc import Callable
from dataclasses import replace
from uuid import UUID, uuid4
from weakref import proxy

from AppKit import (
    NSBackingStoreBuffered,
    NSButton,
    NSColor,
    NSPanel,
    NSView,
    NSWindow,
    NSWindowStyleMaskTitled,
)
from Foundation import NSObject

from src.application.errors import COLLECTION_FAILURE_CODES
from src.application.source_settings import SourceSettingsError
from src.domain.category_configuration import CategoryColumn
from src.domain.source_alerts import (
    COLLECTION_STATE,
    UNBOUND_COLUMN,
    AlertDelivery,
    AlertSchedule,
    ScheduleMode,
    SourceAlertRule,
)
from src.presentation.category_sheet_controls import field, popup, set_entries
from src.presentation.main_list_editor import MainListEditor
from src.presentation.native_views import button, frame, label

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


class AlertRuleActions(NSObject):
    """규칙 하나의 입력과 적용·취소를 시트로 전달한다."""

    owner: "AlertRuleSheet"

    def apply_(self, sender: object) -> None:
        """적용 전에 규칙 전체를 검증한다."""
        self.owner.apply()

    def cancel_(self, sender: object) -> None:
        """이번에 입력한 내용을 버리고 목록으로 돌아간다."""
        self.owner.parent.endSheet_returnCode_(self.owner.window, 0)

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


class AlertRuleSheet:
    """적용할 때까지 모든 입력을 목록에서 분리해 보관한다."""

    def __init__(
        self,
        parent: NSWindow,
        columns: tuple[CategoryColumn, ...],
        original: SourceAlertRule | None,
        people_columns: frozenset[str],
        completed: Callable[[SourceAlertRule | None], None],
    ) -> None:
        """기존 규칙 또는 빈 초안으로 발송 입력과 대상 조건을 함께 연다."""
        self.parent = parent
        self.original = original
        self._completed = completed
        self._result: SourceAlertRule | None = None
        self._columns = (*columns, CategoryColumn(COLLECTION_STATE, "수집 상태", "collection"))
        self._people_columns = people_columns
        self._actions = AlertRuleActions.alloc().init()
        self._actions.owner = proxy(self)
        self.window = NSPanel.alloc().initWithContentRect_styleMask_backing_defer_(
            frame(0, 0, 686, 544), NSWindowStyleMaskTitled, NSBackingStoreBuffered, False
        )
        self.window.setReleasedWhenClosed_(False)
        self.window.setTitle_("알림 규칙 편집" if original else "새 알림 규칙")
        view = NSView.alloc().initWithFrame_(frame(0, 0, 686, 544))
        self.window.setContentView_(view)
        label(view, "규칙 이름", 22, 502, 88)
        self.name = field(view, "", 118, 499, 390, "알림에 표시할 이름")
        self.enabled = NSButton.checkboxWithTitle_target_action_(
            "이 규칙 사용", self._actions, "target:"
        )
        self.enabled.setFrame_(frame(528, 498, 136, 26))
        view.addSubview_(self.enabled)

        label(view, "기준 데이터", 22, 465, 88)
        self.column = popup(view, 118, 461, 546)
        self.column.setTarget_(self._actions)
        self.column.setAction_("column:")
        label(view, "언제 보낼지", 22, 431, 88)
        self.condition = popup(view, 118, 427, 252)
        self.condition.setTarget_(self._actions)
        self.condition.setAction_("condition:")
        self._target_label = label(view, "", 379, 431, 80)
        self.target = field(view, "", 460, 428, 204, "변경 후 값")
        self._target_choice = popup(view, 460, 427, 204)
        self._target_choice.setTarget_(self._actions)
        self._target_choice.setAction_("target:")
        self._schedule_choices: tuple[tuple[ScheduleMode, str], ...] = ()
        self._visible_column: CategoryColumn | None = None
        self._target_values: tuple[str, ...] = ()

        self.scope_view = NSView.alloc().initWithFrame_(frame(22, 146, 642, 266))
        view.addSubview_(self.scope_view)
        self.status = label(view, "", 22, 20, 427)
        self.status.setTextColor_(NSColor.systemRedColor())
        self.scope = MainListEditor(
            self.scope_view,
            columns,
            original.scope if original else None,
            self.input_changed,
            alert_scope=True,
        )
        label(self.scope_view, "어떤 항목에", 0, 234, 104, bold=True)
        self.legacy: NSButton | None = None
        if original and original.item_filter:
            title = (
                "기존 범위 유지 · 회의로 분류된 항목"
                if original.item_filter == "meeting"
                else "기존 범위 유지 · 지나지 않은 일정"
            )
            self.legacy = NSButton.checkboxWithTitle_target_action_(title, self._actions, "target:")
            self.legacy.setFrame_(frame(106, 232, 360, 26))
            self.legacy.setState_(1)
            self.scope_view.addSubview_(self.legacy)
        label(view, "알림 문구", 22, 105, 88)
        self.message = field(view, "", 118, 101, 546, "보낼 본문 입력")
        label(view, "전달 채널", 22, 72, 88)
        self.delivery_channel = popup(view, 118, 68, 546)
        self.delivery_channel.setToolTip_("전체 및 카테고리 설정에서 허용한 채널로 보냅니다.")
        self.delivery_channel.setTarget_(self._actions)
        self.delivery_channel.setAction_("target:")
        for control in (self.name, self.target, self.message):
            control.setDelegate_(self._actions)
        button(view, "취소", 464, 15, 92, self._actions, "cancel:").setKeyEquivalent_("\x1b")
        button(view, "적용", 568, 15, 96, self._actions, "apply:").setKeyEquivalent_("\r")
        self._fill(original)
        self.window.setInitialFirstResponder_(self.name)

    def show(self) -> None:
        """카테고리 설정에서 편집 중인 한 규칙에 집중한다."""
        self.parent.beginSheet_completionHandler_(self.window, self._finish)

    def _finish(self, response: int) -> None:
        """취소하거나 창을 닫으면 편집 전 목록을 유지한다."""
        self.window.orderOut_(None)
        self._completed(self._result if response == 1 else None)

    def apply(self) -> None:
        """항목 조건과 발송 조건이 모두 유효할 때만 결과를 돌려준다."""
        try:
            self._result = self._read_rule()
        except ValueError as error:
            self.status.setStringValue_(str(error))
            self.status.setToolTip_(str(error))
            return
        self.parent.endSheet_returnCode_(self.window, 1)

    def _chosen(self) -> CategoryColumn | None:
        """목록에 실제로 있는 원본 속성만 선택할 수 있다."""
        index = self.column.indexOfSelectedItem() - 1
        return self._columns[index] if 0 <= index < len(self._columns) else None

    def _fill(self, rule: SourceAlertRule | None) -> None:
        """저장한 값 또는 새 규칙을 정확한 원본 속성으로 복원한다."""
        self.name.setStringValue_(rule.name if rule else "")
        set_entries(self.column, ["칼럼 선택", *(column.name for column in self._columns)])
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
        set_entries(self.delivery_channel, titles)
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
        set_entries(self.condition, choices)
        self.scope_view.setHidden_(column is not None and column.id == COLLECTION_STATE)

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
        set_entries(self._target_choice, choices)
        if self._target_values and value in self._target_values:
            self._target_choice.selectItemAtIndex_(self._target_values.index(value) + 1)

    def change_column(self) -> None:
        """속성을 바꾸면 이전 선택형 값을 새 조건에 실수로 쓰지 않는다."""
        if self._visible_column != self._chosen():
            self._load_conditions()
            self._load_target(None)
        self.input_changed()
        self.render_condition()

    def input_changed(self) -> None:
        """현재 입력의 오류 상태만 지운다."""
        self.status.setStringValue_("")
        self.status.setToolTip_(None)

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

    def _read_rule(self) -> SourceAlertRule:
        """입력값으로 검증한 규칙을 구성하고 목록을 건드리지 않는다."""
        name, column = self.name.stringValue().strip(), self._chosen()
        if not name or len(name) > 80 or any(ord(char) < 32 for char in name):
            raise SourceSettingsError("알림 이름을 1~80자로 입력하세요.")
        existing = self.original
        if column is None:
            if existing and existing.column_id == UNBOUND_COLUMN and self.enabled.state() != 1:
                return replace(
                    existing,
                    name=name,
                    delivery=self._read_delivery(),
                    scope=self.scope.draft(),
                    item_filter=existing.item_filter
                    if self.legacy and self.legacy.state() == 1
                    else None,
                )
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
            existing.id if existing else str(uuid4()),
            name,
            column.id,
            column.kind,
            target,
            self.enabled.state() == 1,
            delivery=self._read_delivery(),
            schedule=schedule,
            member=member,
            item_filter=existing.item_filter
            if existing
            and self.legacy
            and self.legacy.state() == 1
            and column.id != COLLECTION_STATE
            else None,
            scope=self.scope.draft() if column.id != COLLECTION_STATE else None,
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
