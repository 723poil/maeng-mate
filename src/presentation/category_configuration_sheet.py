"""카테고리의 저장 칼럼·메인 목록·캘린더·알림을 한 설정 시트에서 관리한다."""

from collections.abc import Callable, Mapping
from dataclasses import replace
from uuid import uuid4
from weakref import proxy

from AppKit import (
    NSBackingStoreBuffered,
    NSButton,
    NSColor,
    NSFont,
    NSImage,
    NSMinYEdge,
    NSPanel,
    NSPopover,
    NSPopoverBehaviorTransient,
    NSPopUpButton,
    NSSegmentedControl,
    NSSegmentSwitchTrackingSelectOne,
    NSTextField,
    NSView,
    NSViewController,
    NSWindow,
    NSWindowStyleMaskTitled,
)
from Foundation import NSObject

from src.application.source_settings import SourceSettingsError
from src.application.work_settings import (
    category_configuration_payload,
    parse_category_configuration,
)
from src.domain.category_configuration import (
    CATEGORY_COLUMN_TYPES,
    CategoryAlert,
    CategoryColumn,
    CategoryConfiguration,
)
from src.domain.category_event_alerts import EventAlertSelection
from src.domain.display_columns import display_line
from src.domain.source_display import SOURCE_COLORS, SourceCategory
from src.presentation.category_sheet_controls import field, popup, set_entries
from src.presentation.column_alert_editor import ColumnAlertEditor
from src.presentation.column_setup_sheet import TYPE_LABELS, scroll
from src.presentation.main_list_editor import MainListEditor
from src.presentation.native_views import button, card, frame, label

BADGE_OPTIONS = (None, False, True)
BADGE_NAMES = ["자동", "텍스트", "상태 배지"]
COLOR_OPTIONS = (None, *SOURCE_COLORS)
COLOR_NAMES = ["기본색", "파랑", "초록", "주황", "보라", "빨강", "청록"]
CHANNEL_OPTIONS: tuple[tuple[str, ...] | None, ...] = (
    None,
    ("macos", "slack"),
    ("macos",),
    ("slack",),
    (),
)
CHANNEL_NAMES = [
    "전체 알림 설정 따름",
    "macOS 알림과 Slack DM",
    "macOS 알림만",
    "Slack DM만",
    "이 카테고리 알림 끄기",
]


class CategorySheetActions(NSObject):
    """카테고리 시트에서 필요한 조작을 편집 초안으로 전달한다."""

    owner: "CategoryConfigurationSheet"

    def tab_(self, sender: NSSegmentedControl) -> None:
        """현재 칼럼 값을 보존하고 선택한 탭을 연다."""
        self.owner.select(sender.selectedSegment())

    def add_(self, sender: object) -> None:
        """빈 칼럼 입력을 표시한다."""
        self.owner.add_column()

    def displayHelp_(self, sender: NSButton) -> None:
        """추가 표시 제목 옆에 도움말을 연다."""
        self.owner.toggle_display_help(sender)

    def remove_(self, sender: NSButton) -> None:
        """저장 칼럼을 초안에서 제거한다."""
        self.owner.remove_column(sender.tag())

    def moveUp_(self, sender: NSButton) -> None:
        """선택 칼럼을 한 칸 위로 옮긴다."""
        self.owner.move_column(sender.tag(), -1)

    def moveDown_(self, sender: NSButton) -> None:
        """선택 칼럼을 한 칸 아래로 옮긴다."""
        self.owner.move_column(sender.tag(), 1)

    def calendar_(self, sender: object) -> None:
        """캘린더가 꺼졌을 때 관련 입력을 접는다."""
        self.owner.render_calendar()

    def save_(self, sender: object) -> None:
        """검증한 카테고리 설정을 저장한다."""
        self.owner.save()

    def cancel_(self, sender: object) -> None:
        """원래 카테고리 설정을 보존하고 닫는다."""
        self.owner.close()


class CategoryConfigurationSheet:
    """공유 스키마를 먼저 만들고 날짜나 알림이 이를 참조하게 한다."""

    def __init__(
        self,
        parent: NSWindow,
        category: SourceCategory,
        apply: Callable[[SourceCategory, tuple[EventAlertSelection, ...]], None],
        closed: Callable[[], None],
        *,
        event_states: Mapping[str, bool] | None = None,
    ) -> None:
        """설정 변경을 외부 저장 전까지 이 시트에 보관한다."""
        self.parent = parent
        self._category, self._apply, self._closed = category, apply, closed
        self._original = category.configuration or CategoryConfiguration()
        self._columns = list(self._original.columns)
        self._rows: list[tuple[NSTextField, NSPopUpButton, NSButton, NSPopUpButton]] = []
        self._calendar_date = self._original.calendar_date
        self._dates: tuple[str, ...] = ()
        self._alert_rules = tuple(item.rule for item in self._original.alerts)
        self._event_alerts = self._original.event_alerts or ()
        self._event_states = dict(event_states or {})
        self._event_changes: dict[str, bool] = {}
        self._alert_editor: ColumnAlertEditor | None = None
        self._display_help = display_help_popover()
        self._tab = 0
        self._actions = CategorySheetActions.alloc().init()
        self._actions.owner = proxy(self)
        self.window = NSPanel.alloc().initWithContentRect_styleMask_backing_defer_(
            frame(0, 0, 686, 583), NSWindowStyleMaskTitled, NSBackingStoreBuffered, False
        )
        self.window.setTitle_("카테고리 설정")
        self.window.setReleasedWhenClosed_(False)
        content = NSView.alloc().initWithFrame_(frame(0, 0, 686, 583))
        self.window.setContentView_(content)
        label(content, category.name, 22, 539, 642, size=17, bold=True)
        titles = ("저장 칼럼", "메인 목록", "캘린더", "알림 규칙")
        self._tabs = NSSegmentedControl.alloc().initWithFrame_(frame(22, 497, 465, 32))
        self._tabs.setSegmentCount_(len(titles))
        self._tabs.setTrackingMode_(NSSegmentSwitchTrackingSelectOne)
        self._tabs.setTarget_(self._actions)
        self._tabs.setAction_("tab:")
        content.addSubview_(self._tabs)
        self._pages = []
        for index, title in enumerate(titles):
            self._tabs.setLabel_forSegment_(title, index)
            self._tabs.setWidth_forSegment_(114, index)
            page = NSView.alloc().initWithFrame_(frame(22, 56, 642, 427))
            content.addSubview_(page)
            self._pages.append(page)
        columns, main_list, calendar, alerts = self._pages
        self._main_editor = MainListEditor(
            main_list, self._original.columns, self._original.main_list, self.clear_status
        )
        label(columns, "카테고리 이름", 10, 387, 128)
        self.name = field(columns, category.name, 141, 383, 483, "카테고리 이름")
        label(
            columns,
            "칼럼을 만든 뒤 수집 소스에서 원본 속성을 연결하세요.",
            10,
            339,
            460,
            secondary=True,
        )
        button(columns, "+ 칼럼 추가", 491, 335, 134, self._actions, "add:")
        self._list = scroll(columns, 0, 0, 642, 318)
        card(calendar, 0, 240, 642, 166)
        self.show_calendar = NSButton.checkboxWithTitle_target_action_(
            "캘린더에 표시", self._actions, "calendar:"
        )
        self.show_calendar.setFrame_(frame(14, 358, 290, 30))
        self.show_calendar.setState_(int(self._original.calendar_visible))
        calendar.addSubview_(self.show_calendar)
        label(calendar, "날짜 기준", 14, 326, 104)
        self.date = popup(calendar, 132, 320, 494)
        label(calendar, "표시 색상", 14, 289, 104)
        self.color = popup(calendar, 132, 283, 494, COLOR_NAMES)
        self.color.selectItemAtIndex_(COLOR_OPTIONS.index(self._original.calendar_color))
        label(
            calendar,
            "캘린더에는 각 표에서 이 칼럼에 연결한 날짜가 표시됩니다.",
            14,
            213,
            610,
            secondary=True,
        )
        self._apply_calendar_to_all = NSButton.checkboxWithTitle_target_action_(
            "카테고리 설정을 모든 소스에 적용", self._actions, "calendar:"
        )
        self._apply_calendar_to_all.setFrame_(frame(14, 171, 414, 25))
        self._apply_calendar_to_all.setHidden_(
            not any(
                item.calendar_visible is not None or item.color is not None
                for item in self._original.inherited
            )
        )
        calendar.addSubview_(self._apply_calendar_to_all)
        label(alerts, "전달 채널", 10, 395, 100)
        self.channels = popup(alerts, 124, 391, 499, CHANNEL_NAMES)
        self.channels.setToolTip_("전체 알림 설정에서 켠 채널로만 보냅니다.")
        old_channels = self._original.channels
        self.channels.selectItemAtIndex_(
            next(
                (
                    i
                    for i, option in enumerate(CHANNEL_OPTIONS)
                    if option is not None
                    and old_channels is not None
                    and set(option) == set(old_channels)
                ),
                0,
            )
        )
        self._alert_body = NSView.alloc().initWithFrame_(frame(0, 0, 642, 380))
        alerts.addSubview_(self._alert_body)
        self.status = label(content, "", 22, 20, 423)
        self.status.setTextColor_(NSColor.systemRedColor())
        button(content, "취소", 464, 15, 92, self._actions, "cancel:").setKeyEquivalent_("\x1b")
        button(content, "저장", 568, 15, 96, self._actions, "save:").setKeyEquivalent_("\r")
        self.render_columns()
        self.update_calendar()
        self.select(0)

    def show(self) -> None:
        """기존 설정 창에 대화상자를 붙인다."""
        self.parent.beginSheet_completionHandler_(self.window, self._complete)

    @property
    def category_key(self) -> str:
        """알림 화면과 같은 카테고리 키로 상태를 갱신한다."""
        return self._category.key

    def update_event_states(self, states: Mapping[str, bool]) -> None:
        """시트에서 바꾼 값을 보존하며 마지막 알림 설정을 보여 준다."""
        self._event_states = dict(states)
        if self._alert_editor is not None:
            self._alert_editor.update_event_states(states)

    def _complete(self, result: int) -> None:
        """시트가 닫힌 뒤 편집기 참조를 해제한다."""
        self._display_help.performClose_(None)
        self.window.orderOut_(None)
        self._closed()

    def close(self) -> None:
        """설정은 건드리지 않고 대화상자를 닫는다."""
        self.parent.endSheet_returnCode_(self.window, 0)

    def columns(self) -> tuple[CategoryColumn, ...]:
        """편집 순서와 표시 여부를 그대로 저장한다."""
        columns = []
        for column, (name, kind_picker, visible, badge) in zip(
            self._columns, self._rows, strict=True
        ):
            kind = CATEGORY_COLUMN_TYPES[kind_picker.indexOfSelectedItem()]
            columns.append(
                replace(
                    column,
                    name=name.stringValue().strip(),
                    kind=kind,
                    visible=visible.state() == 1,
                    status_badge=BADGE_OPTIONS[badge.indexOfSelectedItem()],
                    legacy_kind=column.legacy_kind if kind == column.kind else None,
                )
            )
        return tuple(columns)

    def add_column(self) -> None:
        """카테고리 고유의 새 저장 칸을 추가한다."""
        self._columns = [*self.columns(), CategoryColumn(str(uuid4()), "", "text")]
        self.render_columns(bottom=True)

    def remove_column(self, index: int) -> None:
        """카테고리의 칸을 초안에서 제거한다."""
        self._columns = [item for position, item in enumerate(self.columns()) if position != index]
        self.render_columns()

    def move_column(self, index: int, direction: int) -> None:
        """표시 순서만 재배치하고 칼럼 ID는 유지한다."""
        self._columns = list(self.columns())
        other = index + direction
        if 0 <= index < len(self._columns) and 0 <= other < len(self._columns):
            self._columns[index], self._columns[other] = self._columns[other], self._columns[index]
        self.render_columns()

    def render_columns(self, *, bottom: bool = False) -> None:
        """공유 칸의 이름과 종류를 읽기 쉬운 한 행으로 놓는다."""
        # 표를 다시 그리면 도움말의 기준 버튼도 교체된다.
        self._display_help.performClose_(None)
        old = self._list.documentView()
        offset = (
            max(0, old.frame()[1][1] - 318 - self._list.contentView().bounds()[0][1])
            if old is not None
            else 0
        )
        self._rows.clear()
        height = max(318, 46 + len(self._columns) * 37)
        view = NSView.alloc().initWithFrame_(frame(0, 0, 628, height))
        if not self._columns:
            label(
                view,
                "‘+ 칼럼 추가’를 눌러 이름과 값 종류를 정하세요.",
                10,
                height - 35,
                610,
                secondary=True,
            )
        else:
            for title, x, width in (
                ("칼럼 이름", 10, 170),
                ("값 종류", 190, 94),
                ("추가 표시", 294, 54),
                ("메뉴바 표시", 379, 101),
            ):
                label(view, title, x, height - 32, width, secondary=True)
            help_button = button(
                view, "", 347, height - 36, 24, self._actions, "displayHelp:", bordered=False
            )
            help_button.setImage_(
                NSImage.imageWithSystemSymbolName_accessibilityDescription_(
                    "questionmark.circle", "추가 표시 도움말"
                )
            )
            help_button.setContentTintColor_(NSColor.secondaryLabelColor())
            help_button.setAccessibilityLabel_("추가 표시 도움말")
        for index, item in enumerate(self._columns):
            y = height - 67 - index * 37
            name = field(view, item.name, 10, y, 170, "예: 검토일")
            kind = popup(view, 189, y, 95, [TYPE_LABELS[value] for value in CATEGORY_COLUMN_TYPES])
            kind.selectItemAtIndex_(CATEGORY_COLUMN_TYPES.index(item.kind))
            visible = NSButton.checkboxWithTitle_target_action_("", self._actions, "calendar:")
            visible.setAccessibilityLabel_(
                f"{item.name} 칼럼 추가 표시" if item.name else "추가 표시"
            )
            visible.setFrame_(frame(307, y, 32, 24))
            visible.setState_(int(item.visible))
            view.addSubview_(visible)
            badge = popup(view, 374, y, 102, BADGE_NAMES)
            badge.selectItemAtIndex_(BADGE_OPTIONS.index(item.status_badge))
            badge.setAccessibilityLabel_(f"{item.name} 메뉴바 표시")
            badge.setToolTip_(
                "자동은 원본 상태 속성이나 업무 상태에 연결한 칼럼을 배지로 표시합니다."
            )
            self._rows.append((name, kind, visible, badge))
            for x, title, action, enabled in (
                (487, "↑", "moveUp:", index > 0),
                (531, "↓", "moveDown:", index + 1 < len(self._columns)),
                (575, "−", "remove:", True),
            ):
                control = button(view, title, x, y, 36, self._actions, action)
                control.setTag_(index)
                control.setEnabled_(enabled)
        self._list.setDocumentView_(view)
        view.scrollPoint_((0, 0 if bottom else max(0, height - 318 - offset)))

    def toggle_display_help(self, anchor: NSButton) -> None:
        """물음표를 다시 누르면 도움말을 닫는다."""
        if self._display_help.isShown():
            self._display_help.performClose_(None)
        else:
            self._display_help.showRelativeToRect_ofView_preferredEdge_(
                anchor.bounds(), anchor, NSMinYEdge
            )

    def update_calendar(self) -> None:
        """현재 카테고리에서 정의한 날짜 칼럼만 표시 기준으로 선택한다."""
        dates = tuple(item for item in self._columns if item.kind == "date" and item.name)
        self._dates = tuple(item.id for item in dates)
        set_entries(self.date, ["소스의 일정·기한 사용", *(item.name for item in dates)])
        if self._calendar_date in self._dates:
            self.date.selectItemAtIndex_(self._dates.index(self._calendar_date) + 1)
        self.render_calendar()

    def render_calendar(self) -> None:
        """표시 여부와 날짜·색 설정의 관계를 보여 준다."""
        enabled = self.show_calendar.state() == 1
        self.date.setEnabled_(enabled)
        self.color.setEnabled_(enabled)

    def update_alerts(self) -> None:
        """소스와 무관하게 공유 칼럼에서 규칙의 조건을 선택한다."""
        self._alert_body.setSubviews_([])
        columns = tuple(item for item in self._columns if item.name)
        editor = ColumnAlertEditor(
            self._alert_body,
            columns,
            self._alert_rules,
            False,
            self.clear_status,
            events=self._event_alerts,
            event_states=self._event_states,
            event_changes=self._event_changes,
        )
        editor.set_scope_description(
            "이 카테고리에 연결한 표의 알림입니다. 사람 필터를 켠 표에서는 내 항목만 확인합니다."
        )
        self._alert_editor = editor

    def clear_status(self) -> None:
        """다른 입력을 시작하면 이전 오류를 지운다."""
        self.status.setStringValue_("")
        self.status.setToolTip_(None)

    def read_tab(self) -> None:
        """탭을 떠나도 현재 선택을 잃지 않는다."""
        if self._tab == 0:
            self._columns = list(self.columns())
        elif self._tab == 2:
            index = self.date.indexOfSelectedItem() - 1
            if index >= 0:
                self._calendar_date = self._dates[index]
            else:
                self._calendar_date = None
        elif self._tab == 3 and self._alert_editor is not None:
            self._alert_rules = self._alert_editor.draft()
            self._event_alerts, self._event_changes = self._alert_editor.event_draft()

    def select(self, index: int) -> None:
        """유효하지 않은 편집을 버리지 않고 현재 탭에 안내한다."""
        try:
            self.read_tab()
        except SourceSettingsError as error:
            self.status.setStringValue_(str(error))
            self.status.setToolTip_(str(error))
            self._tabs.setSelectedSegment_(self._tab)
            return
        if index != 0:
            self._display_help.performClose_(None)
        self._tab = index
        self._tabs.setSelectedSegment_(index)
        for position, page in enumerate(self._pages):
            page.setHidden_(index != position)
        if index == 1:
            self._main_editor.update_columns(tuple(self._columns))
        elif index == 2:
            self.update_calendar()
        elif index == 3:
            self.update_alerts()
        self.clear_status()

    def draft_category(self) -> SourceCategory:
        """현재 시트의 변경값을 검증하고 저장 가능한 카테고리를 만든다."""
        self.read_tab()
        changed_calendar = (
            self.show_calendar.state() == 1,
            COLOR_OPTIONS[self.color.indexOfSelectedItem()],
        ) != (self._original.calendar_visible, self._original.calendar_color)
        channels = CHANNEL_OPTIONS[self.channels.indexOfSelectedItem()]
        overrides = tuple(
            replace(item, calendar_visible=None, color=None)
            if changed_calendar or self._apply_calendar_to_all.state() == 1
            else item
            for item in self._original.inherited
        )
        if channels != self._original.channels:
            overrides = tuple(replace(item, channels=None) for item in overrides)
        before = {column.id: column.visible for column in self._original.columns}
        changed = {column.id for column in self._columns if before.get(column.id) != column.visible}
        overrides = tuple(
            replace(
                item,
                hidden_columns=tuple(key for key in item.hidden_columns if key not in changed),
            )
            for item in overrides
        )
        current_ids = {column.id for column in self._columns}
        common = current_ids.intersection(before)
        if tuple(key for key in before if key in common) != tuple(
            column.id for column in self._columns if column.id in common
        ):
            overrides = tuple(replace(item, column_order=None) for item in overrides)
        old_scopes = {entry.rule.id: entry.sources for entry in self._original.alerts}
        # 다른 탭에서 칼럼을 지우거나 종류를 바꿔도 참조 중인 조건을 직접 고칠 수 있다.
        self._main_editor.update_columns(tuple(self._columns))
        try:
            main_list = self._main_editor.draft()
        except ValueError as error:
            self.select(1)
            raise SourceSettingsError(str(error)) from error
        proposed = CategoryConfiguration(
            tuple(self._columns),
            self.show_calendar.state() == 1,
            COLOR_OPTIONS[self.color.indexOfSelectedItem()],
            self._calendar_date,
            channels,
            tuple(CategoryAlert(rule, old_scopes.get(rule.id)) for rule in self._alert_rules),
            overrides,
            self._event_alerts,
            main_list=main_list,
        )
        name = self.name.stringValue().strip()
        if not name or len(name) > 80:
            raise SourceSettingsError("카테고리 이름을 1~80자로 입력하세요.")
        try:
            configuration = parse_category_configuration(category_configuration_payload(proposed))
        except ValueError:
            raise SourceSettingsError("칼럼 이름과 날짜·알림에 쓸 칼럼을 확인하세요.") from None
        return replace(self._category, name=name, configuration=configuration)

    def save(self) -> None:
        """기존 예외를 보존하고 직접 바꾼 항목만 저장한다."""
        try:
            category = self.draft_category()
        except SourceSettingsError as error:
            self.status.setStringValue_(str(error))
            self.status.setToolTip_(str(error))
            return
        selections = tuple(
            EventAlertSelection(category.id, trigger, enabled)
            for trigger, enabled in self._event_changes.items()
        )
        self._apply(category, selections)
        self.close()


def display_help_popover() -> NSPopover:
    """체크한 칼럼의 표시 위치와 숨긴 칼럼의 용도를 안내한다."""
    width, height = 298.0, 158.0
    content = NSView.alloc().initWithFrame_(frame(0, 0, width, height))
    label(content, "추가 표시", 16, 126, width - 32, size=14, bold=True)
    label(content, "표시 예시", 16, 49, 62, secondary=True)
    label(content, display_line("항목 제목", (("장소", "3층"),)), 82, 49, width - 98, bold=True)
    for text, y, lines_height, secondary in (
        (
            "체크한 칼럼은 메뉴바와 캘린더 상세에 표시돼요. 메뉴바에서 상태 배지를 고르면 값이 제목 앞에 나와요.",
            76,
            46,
            False,
        ),
        ("숨긴 칼럼도 캘린더 날짜나 알림 조건에 쓸 수 있어요.", 10, 34, True),
    ):
        paragraph = NSTextField.wrappingLabelWithString_(text)
        paragraph.setFont_(NSFont.systemFontOfSize_(12))
        paragraph.setTextColor_(
            NSColor.secondaryLabelColor() if secondary else NSColor.labelColor()
        )
        paragraph.setFrame_(frame(16, y, width - 32, lines_height))
        content.addSubview_(paragraph)
    controller = NSViewController.alloc().init()
    controller.setView_(content)
    popover = NSPopover.alloc().init()
    popover.setContentViewController_(controller)
    popover.setContentSize_((width, height))
    popover.setBehavior_(NSPopoverBehaviorTransient)
    popover.setAnimates_(False)
    return popover
