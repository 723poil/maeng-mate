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
    NSBox,
    NSBoxSeparator,
    NSButton,
    NSImage,
    NSScrollView,
    NSTextField,
    NSView,
)
from Foundation import NSObject

from src.application.source_settings import CategoryDraft, SourceSettingsClient
from src.application.work_settings import (
    category_bindings,
    category_documents,
    parse_categories,
    parse_source,
)
from src.domain.category_configuration import CategoryConfiguration
from src.domain.event_classification import EVENT_CLASSIFICATION
from src.domain.source_display import SourceCategory
from src.presentation.alert_source_scope_editor import AlertSource
from src.presentation.category_configuration_sheet import CategoryConfigurationSheet
from src.presentation.native_views import button, card, frame, label, place_label
from src.presentation.preferences_controller import PreferencesController
from src.presentation.source_settings_form import SourceEditor


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
        """이름과 연결 수를 읽는 흐름과 오른쪽 조작을 분리한다."""
        previous_height = self._document.frame()[1][1]
        origin = self._page.contentView().bounds()[0][1]
        offset = max(0.0, previous_height - self._viewport - origin)
        self.rows.clear()
        self._document.setSubviews_([])
        panel_height = 54 + max(1, len(self._categories)) * 42
        height = max(self._viewport, panel_height + 16)
        self._document.setFrameSize_((584, height))
        card(self._document, 16, height - panel_height - 8, 552, panel_height)
        label(self._document, "등록 카테고리", 28, height - 38, 200, bold=True)
        self._add = button(self._document, "+ 추가", 454, height - 42, 100, self._actions, "add:")
        if not self._categories:
            label(
                self._document,
                "카테고리를 추가하면 수집 소스에서 선택할 수 있습니다.",
                28,
                height - 82,
                520,
                secondary=True,
            )
        for index, item in enumerate(self._categories):
            top = height - 59 - index * 42
            separator = NSBox.alloc().initWithFrame_(frame(28, top, 528, 1))
            separator.setBoxType_(NSBoxSeparator)
            self._document.addSubview_(separator)
            name = label(self._document, item.name, 28, top - 40, 248, size=13)
            place_label(name, 28, top - 40, 248, 40)
            name.setToolTip_(item.name)
            used = self._usage[item.id]
            count = label(
                self._document,
                f"소스 {used}개" if used else "연결 없음",
                288,
                top - 40,
                94,
                secondary=True,
            )
            place_label(count, 288, top - 40, 94, 40)
            rename = button(self._document, "설정…", 391, top - 34, 88, self._actions, "rename:")
            remove = button(self._document, "삭제", 490, top - 34, 64, self._actions, "remove:")
            for control in (rename, remove):
                control.setTag_(index)
            if used:
                remove.setToolTip_(
                    "연결된 소스를 다른 카테고리로 옮기거나 제거한 뒤 이 카테고리를 삭제할 수 있습니다."
                )
            self.rows.append(CategoryRow(rename, remove, used))
        self._document.scrollPoint_((0, max(0, height - self._viewport - offset)))

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
