"""카테고리 목록과 이름 편집을 분리한 macOS 설정이다."""

from collections import Counter
from dataclasses import dataclass, replace
from urllib.parse import unquote
from uuid import uuid4

from AppKit import (
    NSAlert,
    NSAlertFirstButtonReturn,
    NSAlertSecondButtonReturn,
    NSAlertStyleWarning,
    NSButton,
    NSImage,
    NSLineBreakByTruncatingTail,
    NSScrollView,
    NSTextAlignmentRight,
    NSTextField,
    NSView,
)
from Foundation import NSObject

from src.application.category_settings_codec import (
    category_bindings,
    category_documents,
    parse_categories,
)
from src.application.source_settings import CategoryDraft, SourceSettingsClient
from src.application.source_settings_codec import parse_source
from src.domain.category_configuration import CategoryConfiguration
from src.domain.event_classification import EVENT_CLASSIFICATION
from src.domain.source_display import SourceCategory
from src.presentation.alert_source_scope_editor import AlertSource
from src.presentation.category_configuration_sheet import CategoryConfigurationSheet
from src.presentation.native_views import (
    BUTTON_BEZEL_INSET,
    CARD_PADDING,
    CARD_TITLE,
    CARD_WIDTH,
    FORM_ROW,
    button,
    card,
    card_height,
    form_control_y,
    form_dividers,
    form_row_y,
    frame,
    icon_button,
    label,
    place_label,
)
from src.presentation.preferences_controller import PreferencesController
from src.presentation.source_settings_form import SourceEditor

# 페이지 맨 위 카드와 스크롤 영역 위쪽 사이 여백이다.
PAGE_TOP = 8.0
# 추가·삭제 아이콘 버튼은 근무 규칙의 휴게 줄과 같은 26pt 정사각형이다.
ICON_SIZE = 26.0
# 줄 오른쪽 조작과 연결 수 사이의 눈에 보이는 간격이다.
CONTROL_GAP = 8.0
SETTINGS_WIDTH = 80.0
COUNT_WIDTH = 96.0
# 삭제 아이콘은 카드 오른쪽 여백에 붙이고, 설정 버튼은 테두리 기준으로 그 왼쪽에 둔다.
REMOVE_X = CARD_WIDTH - CARD_PADDING - ICON_SIZE
SETTINGS_X = REMOVE_X - CONTROL_GAP - SETTINGS_WIDTH + BUTTON_BEZEL_INSET
# 연결 수는 설정 버튼 테두리 왼쪽에 오른쪽 정렬하고, 이름은 남은 폭을 쓴다.
COUNT_X = SETTINGS_X + BUTTON_BEZEL_INSET - CONTROL_GAP - COUNT_WIDTH
NAME_WIDTH = COUNT_X - CONTROL_GAP - CARD_PADDING


@dataclass(frozen=True)
class CategoryRow:
    """목록의 조작과 연결된 소스 수를 보존한다."""

    rename: NSButton
    remove: NSButton
    used: int


class CategoryNameInput(NSObject):
    """중복과 빈 이름을 입력하는 동안 알린다."""

    field: NSTextField
    hint: NSTextField
    save: NSButton
    taken: frozenset[str]

    def controlTextDidChange_(self, notification: object) -> None:
        """다른 화면에 나가지 않고 유효한 이름으로 고칠 수 있게 한다."""
        self.update()

    def update(self) -> None:
        """입력값을 검증하고 저장 버튼과 오류를 함께 갱신한다."""
        name = self.field.stringValue().strip()
        invalid = len(name) > 80 or any(ord(char) < 32 for char in name)
        duplicate = name.casefold() in self.taken
        self.save.setEnabled_(bool(name) and not invalid and not duplicate)
        self.hint.setStringValue_(
            "이미 등록된 이름입니다."
            if duplicate
            else "이름은 80자 이내로 입력하세요."
            if invalid
            else ""
        )


def prompt_category_name(title: str, old_name: str, taken: frozenset[str]) -> str | None:
    """카테고리 이름 하나만 입력하는 기본 macOS 대화상자를 연다."""
    dialog = NSAlert.alloc().init()
    dialog.setMessageText_(title)
    icon = NSImage.imageWithSystemSymbolName_accessibilityDescription_("tag", "카테고리")
    if icon is not None:
        dialog.setIcon_(icon)
    accessory = NSView.alloc().initWithFrame_(frame(0, 0, 260, 48))
    entry = NSTextField.textFieldWithString_(old_name)
    entry.setFrame_(frame(0, 24, 260, 24))
    entry.setPlaceholderString_("카테고리 이름")
    accessory.addSubview_(entry)
    dialog.setAccessoryView_(accessory)
    actions = CategoryNameInput.alloc().init()
    actions.field, actions.taken = entry, taken
    actions.hint = label(accessory, "", 0, 0, 260, secondary=True)
    actions.save = dialog.addButtonWithTitle_("저장")
    dialog.addButtonWithTitle_("취소")
    entry.setDelegate_(actions)
    actions.update()
    dialog.window().setInitialFirstResponder_(entry)
    return entry.stringValue().strip() if dialog.runModal() == NSAlertFirstButtonReturn else None


def confirm_removal(name: str) -> bool:
    """연결 없는 카테고리를 지울 때 실제 삭제 여부를 한 번 확인한다."""
    dialog = NSAlert.alloc().init()
    dialog.setAlertStyle_(NSAlertStyleWarning)
    dialog.setMessageText_("카테고리를 삭제할까요?")
    dialog.setInformativeText_(name)
    dialog.addButtonWithTitle_("취소")
    dialog.addButtonWithTitle_("삭제")
    return dialog.runModal() == NSAlertSecondButtonReturn


class CategoryActions(NSObject):
    """목록의 버튼을 카테고리 편집 동작에 연결한다."""

    owner: "CategorySettingsForm"

    def add_(self, sender: object) -> None:
        """카테고리 이름을 새로 입력한다."""
        self.owner.edit_name(None)

    def rename_(self, sender: NSButton) -> None:
        """현재 이름을 불러오고 같은 ID를 사용한다."""
        self.owner.edit_configuration(sender.tag())

    def remove_(self, sender: NSButton) -> None:
        """선택한 카테고리에만 삭제를 요청한다."""
        self.owner.remove(sender.tag())


class CategorySettingsForm:
    """수집 소스가 없는 카테고리도 목록에서 관리한다."""

    def __init__(
        self,
        page: NSScrollView,
        viewport: float,
        editor: SourceEditor,
        preferences: PreferencesController | None = None,
        client: SourceSettingsClient | None = None,
    ) -> None:
        """카테고리 명령은 공유 설정의 버전으로 저장한다."""
        self.editor, self._client = editor, client
        self._preferences = preferences
        self._page, self._viewport = page, viewport
        self._generation = -1
        self._usage: Counter[str] = Counter()
        self._categories: tuple[SourceCategory, ...] = ()
        self._pending_name: SourceCategory | None = None
        self._sheet: CategoryConfigurationSheet | None = None
        self._refresh_preferences_after: int | None = None
        self.rows: list[CategoryRow] = []
        self.error: str | None = None
        self._actions = CategoryActions.alloc().init()
        self._actions.owner = self
        self._document = NSView.alloc().initWithFrame_(frame(0, 0, 584, viewport))
        page.setDocumentView_(self._document)
        self._add: NSButton
        self._draw()

    def _draw(self) -> None:
        """카테고리 한 줄에 이름·연결 수·설정·삭제를 두고 카드 높이를 목록 길이에 맞춘다."""
        previous_height = self._document.frame()[1][1]
        origin = self._page.contentView().bounds()[0][1]
        offset = max(0.0, previous_height - self._viewport - origin)
        self.rows.clear()
        self._document.setSubviews_([])
        # 카테고리가 없어도 안내 한 줄이 들어가도록 최소 한 줄 높이를 둔다.
        rows = max(1, len(self._categories))
        panel_height = card_height(rows)
        height = max(self._viewport, PAGE_TOP + panel_height + CARD_PADDING)
        self._document.setFrameSize_((584, height))
        panel = NSView.alloc().initWithFrame_(
            frame(CARD_PADDING, height - PAGE_TOP - panel_height, CARD_WIDTH, panel_height)
        )
        self._document.addSubview_(panel)
        card(panel, 0, 0, CARD_WIDTH, panel_height)
        form_dividers(panel, panel_height, len(self._categories))
        title = label(panel, "내 카테고리", 0, 0, 300, size=13, bold=True)
        place_label(title, CARD_PADDING, panel_height - CARD_PADDING - CARD_TITLE, 300, CARD_TITLE)
        # 추가 아이콘은 제목 줄 높이의 가운데에 둔다.
        title_button_y = panel_height - CARD_PADDING - CARD_TITLE + (CARD_TITLE - ICON_SIZE) / 2
        self._add = icon_button(
            panel, "plus", "카테고리 추가", REMOVE_X, title_button_y, self._actions, "add:"
        )
        self._add.setFrame_(frame(REMOVE_X, title_button_y, ICON_SIZE, ICON_SIZE))
        if not self._categories:
            # 빈 목록은 빈 줄 대신 다음에 할 일을 첫 줄에 한 번만 알린다.
            hint_width = CARD_WIDTH - 2 * CARD_PADDING
            hint = label(
                panel,
                "카테고리를 추가하면 수집 소스에서 고를 수 있습니다.",
                0,
                0,
                hint_width,
                secondary=True,
            )
            place_label(hint, CARD_PADDING, form_row_y(panel_height, 0), hint_width, FORM_ROW)
        for index, item in enumerate(self._categories):
            self.rows.append(self._row(panel, index, item, form_row_y(panel_height, index)))
        self._document.scrollPoint_((0, max(0, height - self._viewport - offset)))

    def _row(self, panel: NSView, index: int, item: SourceCategory, y: float) -> CategoryRow:
        """이름은 왼쪽, 연결 수는 설정 버튼 앞, 삭제 아이콘은 카드 오른쪽 끝에 둔다."""
        name = label(panel, item.name, 0, 0, NAME_WIDTH, size=13)
        # 긴 이름은 한 줄에서 말줄임하고 전체 이름은 도움말로 보여 준다.
        name.cell().setLineBreakMode_(NSLineBreakByTruncatingTail)
        place_label(name, CARD_PADDING, y, NAME_WIDTH, FORM_ROW)
        name.setToolTip_(item.name)
        used = self._usage[item.id]
        count = label(
            panel, f"표 {used}개 연결" if used else "연결한 표 없음", 0, 0, 0, secondary=True
        )
        count.setAlignment_(NSTextAlignmentRight)
        place_label(count, COUNT_X, y, COUNT_WIDTH, FORM_ROW)
        # 설정 버튼과 삭제 아이콘은 모두 26pt 높이라 같은 y로 줄 가운데에 둔다.
        control_y = form_control_y(y, ICON_SIZE)
        rename = button(
            panel, "설정…", SETTINGS_X, control_y, SETTINGS_WIDTH, self._actions, "rename:"
        )
        rename.setAccessibilityLabel_(f"{item.name} 설정")
        remove = icon_button(
            panel,
            "minus.circle",
            f"{item.name} 삭제",
            REMOVE_X,
            control_y,
            self._actions,
            "remove:",
        )
        remove.setFrame_(frame(REMOVE_X, control_y, ICON_SIZE, ICON_SIZE))
        for control in (rename, remove):
            control.setTag_(index)
        if used:
            # 연결한 표가 있으면 삭제를 막고, 흐린 아이콘 위에서 푸는 방법을 알려 준다.
            remove.setToolTip_(
                "연결한 표를 다른 카테고리로 옮기거나 연결을 해제하면 삭제할 수 있습니다."
            )
        return CategoryRow(rename, remove, used)

    def edit_name(self, index: int | None) -> None:
        """추가와 이름 변경은 같은 대화상자에서 한 번에 저장한다."""
        if not self.editor.editable:
            return
        current = self._categories[index] if index is not None else None
        taken = frozenset(item.name.casefold() for item in self._categories if item != current)
        pending = self._pending_name
        same_edit = pending is not None and (
            current.id == pending.id
            if current
            else all(item.id != pending.id for item in self._categories)
        )
        initial = (
            pending.name if same_edit and pending is not None else current.name if current else ""
        )
        name = prompt_category_name(
            "카테고리 이름 변경" if current else "새 카테고리", initial, taken
        )
        if name is not None:
            self.save_name(index, name)

    def save_name(self, index: int | None, name: str) -> None:
        """연결을 바꾸지 않고 목록의 이름만 갱신한다."""
        categories = list(self._categories)
        if index is None:
            item = SourceCategory(str(uuid4()), name, CategoryConfiguration())
            categories.append(item)
        else:
            item = replace(categories[index], name=name)
            categories[index] = item
        if tuple(categories) != self._categories:
            self._pending_name = item
        self._save(tuple(categories))

    def edit_configuration(self, index: int) -> None:
        """저장 칼럼·캘린더·알림을 관리하는 별도 시트를 연다."""
        parent = self._document.window()
        if parent is None or self._sheet is not None or not self.editor.editable:
            return
        current = self._categories[index]

        def apply(category: SourceCategory) -> None:
            """선택한 카테고리만 새 설정으로 교체한다."""
            self._save(
                tuple(category if item.id == current.id else item for item in self._categories),
            )

        def closed() -> None:
            """시트가 닫히면 다음 설정을 다시 열 수 있다."""
            self._sheet = None

        state = self.editor.state
        people: set[str] = set()
        sources: list[AlertSource] = []
        for raw in state.settings.sources if state else ():
            if raw.get("categoryId") == current.id:
                mapping = parse_source(raw)
                bindings = category_bindings(raw)
                labels = raw.get("columnLabels")
                sources.append(
                    AlertSource(
                        mapping.source_id,
                        str(raw.get("name") or f"소스 {len(sources) + 1}"),
                        mapping.provider,
                        mapping.round_title_property is not None,
                        frozenset(bindings)
                        | ({EVENT_CLASSIFICATION} if mapping.kind == "meeting" else set()),
                        {
                            unquote(identifier): name
                            for role, identifier in mapping.properties.items()
                            if isinstance(labels, dict)
                            and isinstance(name := labels.get(role), str)
                        },
                        origin_id=mapping.origin,
                    )
                )
                roles = raw.get("properties")
                person_properties = (
                    {
                        v
                        for k, v in roles.items()
                        if k in {"people", "assignees", "participants"} and isinstance(v, str)
                    }
                    if isinstance(roles, dict)
                    else set()
                )
                people.update(
                    key
                    for key, binding in bindings.items()
                    if binding.kind == "people" or binding.property_id in person_properties
                )
        preferences = self._preferences.value if self._preferences else None
        self._sheet = CategoryConfigurationSheet(
            parent,
            current,
            apply,
            closed,
            people_columns=frozenset(people),
            sources=tuple(sources),
            client=self._client,
            global_channels=preferences.channels if preferences is not None else (),
        )
        self._sheet.show()

    def remove(self, index: int) -> None:
        """연결이 없는 카테고리만 사용자 확인 뒤 삭제한다."""
        if not self.editor.editable or not 0 <= index < len(self.rows) or self.rows[index].used:
            return
        item = self._categories[index]
        if confirm_removal(item.name):
            self._save(tuple(category for category in self._categories if category.id != item.id))

    def _save(
        self,
        categories: tuple[SourceCategory, ...],
    ) -> None:
        """검증된 카테고리 목록만 한 번 제출한다."""
        state = self.editor.state
        if state is None or not self.editor.editable or categories == self._categories:
            return
        try:
            parse_categories(category_documents(categories))
        except ValueError:
            self.error = "다른 카테고리와 겹치지 않는 이름을 1~80자로 입력하세요."
            return
        self.error = None
        self.editor.edited()
        if {item.id for item in self._categories} - {item.id for item in categories}:
            self._refresh_preferences_after = self.editor.generation
        self.editor.save(CategoryDraft(state.settings.revision, categories))
        self.render()

    def render(self) -> None:
        """저장 성공 뒤 목록을 갱신하며 입력 실패 때는 기존 목록을 보존한다."""
        state = self.editor.state
        if state is not None and self._generation != self.editor.generation:
            self._generation = self.editor.generation
            self._categories = state.categories
            if self._pending_name in self._categories:
                self._pending_name = None
            self._usage = Counter(
                value
                for raw in state.settings.sources
                if isinstance(value := raw.get("categoryId"), str)
            )
            self.error = None
            self._draw()
        self._add.setEnabled_(self.editor.editable)
        for row in self.rows:
            row.rename.setEnabled_(self.editor.editable)
            row.remove.setEnabled_(self.editor.editable and not row.used)

    @property
    def awaiting_alert_preferences(self) -> bool:
        """카테고리에서 바꾼 공용 스위치를 다시 읽기 전에는 편집을 막는다."""
        return self._refresh_preferences_after is not None

    def refresh_alert_preferences(self) -> None:
        """규칙 저장이 끝나면 알림 화면을 같은 스위치 값으로 다시 읽는다."""
        previous = self._refresh_preferences_after
        if previous is None or self.editor.busy:
            return
        if self.editor.generation <= previous and self.editor.error is None:
            return
        preferences = self._preferences
        if preferences is None:
            self._refresh_preferences_after = None
        elif not preferences.busy and not preferences.dirty:
            preferences.reload()
            self._refresh_preferences_after = None
