"""수집 소스를 선택하고 실제 칼럼에 앱 필드를 연결하는 Cocoa 폼이다."""

from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from urllib.parse import unquote

from AppKit import NSButton, NSMenuItem, NSPopUpButton, NSScrollView, NSTextField, NSView
from Foundation import NSObject

from src.application.notion_page_links import PageLinksError, page_url
from src.application.source_settings import (
    ROLES,
    CollectionDraft,
    ColumnRole,
    SourceColumn,
    SourceDraft,
    SourceSchema,
    SourceSettingsClient,
    SourceSettingsError,
    SourceSettingsState,
)
from src.application.work_settings import category_bindings, column_bindings_payload
from src.domain.category_configuration import ColumnBinding
from src.domain.models import SourceKind
from src.domain.source_display import SOURCE_SORTS, SourceCategory
from src.presentation.column_setup_sheet import ColumnSetupSheet
from src.presentation.native_views import button, card, frame, label
from src.presentation.runtime_settings_controller import SettingsEditor

type SourceEditor = SettingsEditor[CollectionDraft, SourceSettingsState]
HEADER_HEIGHT = 232
OPTION_HEIGHT = 155
COLLAPSED_OPTIONS = 44
EVENT_CHOICES = ("other", "meeting", "leave")
COLLECTION_MODES: tuple[SourceKind, ...] = ("task", "meeting", "leave")


def set_titles(control: NSPopUpButton, titles: list[str]) -> None:
    """같은 표시명이라도 원래 배열과 같은 수의 선택 항목을 유지한다."""
    control.removeAllItems()
    for title in titles:
        item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(title, None, "")
        control.menu().addItem_(item)
    control.selectItemAtIndex_(0)


def source_menu_name(raw: dict[str, object], position: int) -> str:
    """소스명을 표시하고 아직 카테고리가 필요한 연결을 표시한다."""
    name = str(raw.get("name") or f"소스 {position}")
    return name if raw.get("categoryId") else f"{name} · 카테고리 필요"


class SourceFormActions(NSObject):
    """사용자가 누른 소스·조회·매핑 조작을 전달한다."""

    owner: "SourceSettingsForm"

    def select_(self, sender: object) -> None:
        """다른 소스를 열 때 초안이 남았는지 확인한다."""
        self.owner.select()

    def category_(self, sender: object) -> None:
        """카테고리가 바뀌면 이전 칼럼의 원본 연결을 갱신한다."""
        self.owner.change_category()

    def structure_(self, sender: object) -> None:
        """하위 표 모드를 바꾸면 사용할 스키마를 다시 조회하게 한다."""
        self.owner.change_structure()

    def mode_(self, sender: object) -> None:
        """선택한 수집 방식의 원본 연결을 연다."""
        self.owner.change_mode()

    def newSource_(self, sender: object) -> None:
        """빈 연결 폼에서 새 수집 소스를 설정한다."""
        self.owner.new_source()

    def manageCategories_(self, sender: object) -> None:
        """설정 사이드바의 카테고리 관리로 이동한다."""
        self.owner.open_categories()

    def options_(self, sender: object) -> None:
        """필요한 경우에만 세부 컨트롤을 펼친다."""
        self.owner.toggle_options()

    def changed_(self, sender: object) -> None:
        """현재 입력만 초안으로 표시한다."""
        self.owner.mark_edited()

    def controlTextDidChange_(self, notification: object) -> None:
        """텍스트 변경도 초안으로 관리한다."""
        self.owner.mark_edited()

    def inspect_(self, sender: object) -> None:
        """명시적으로 칼럼을 요청한다."""
        self.owner.inspect()

    def setupColumns_(self, sender: object) -> None:
        """표시와 동작을 전용 시트에서 정한다."""
        self.owner.open_column_setup()

    def schema_(self, sender: object) -> None:
        """선택한 표의 칼럼만 연다."""
        self.owner.choose_schema()

    def columns_(self, sender: object) -> None:
        """상태나 유형 칼럼이 바뀌면 선택값을 다시 구성한다."""
        self.owner.change_columns()

    def rules_(self, sender: object) -> None:
        """기존 데이터 분류의 원본 쪽 매핑을 편집한다."""
        self.owner.mark_edited()

    def remove_(self, sender: object) -> None:
        """저장 전에 제거를 지정하거나 되돌린다."""
        self.owner.toggle_removal()


class SourceSettingsForm:
    """탭을 바꾸거나 렌더링을 반복해도 자동으로 외부 조회하지 않는다."""

    def __init__(
        self,
        page: NSScrollView,
        viewport: float,
        editor: SourceEditor,
        client: SourceSettingsClient,
        open_categories: Callable[[], None] | None = None,
    ) -> None:
        """컬렉션 편집과 칼럼 조회의 상태를 나눠 구성한다."""
        self.editor, self._client, self._viewport = editor, client, viewport
        self._page = page
        self.open_categories = open_categories if open_categories is not None else lambda: None
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="source-columns")
        self._pending: Future[tuple[SourceSchema, ...]] | None = None
        self._generation = -1
        self._selected = 0
        self._original: dict[str, object] = {}
        self._profiles: tuple[SourceSchema, ...] = ()
        self._schema: SourceSchema | None = None
        self._lookup_reference = ""
        self._pending_reference = ""
        self._remove_pending = False
        self._categories: tuple[SourceCategory, ...] = ()
        self._options_open = False
        self._body_height = 0.0
        self._mapping: dict[str, tuple[NSPopUpButton, tuple[SourceColumn, ...]]] = {}
        self._options: dict[str, NSButton | NSPopUpButton] = {}
        self._choices: dict[str, int] = {}
        self._body: NSView | None = None
        self._role_body: NSView | None = None
        self._column_panel: ColumnSetupSheet | None = None
        self._category_links: dict[str, ColumnBinding] = {}
        self._open_after_inspect = False
        self.error: str | None = None
        self._actions = SourceFormActions.alloc().init()
        self._actions.owner = self
        self.document = NSView.alloc().initWithFrame_(frame(0, 0, 584, viewport))
        page.setDocumentView_(self.document)
        self._header = NSView.alloc().initWithFrame_(frame(16, 0, 552, HEADER_HEIGHT))
        self.document.addSubview_(self._header)
        card(self._header, 0, 0, 552, HEADER_HEIGHT)
        label(self._header, "수집 소스", 12, 194, 85, bold=True)
        self.list = self._popup(self._header, 102, 190, 320, ["새 소스"], "select:")
        self._new = button(self._header, "+ 새 소스", 430, 190, 108, self._actions, "newSource:")
        label(self._header, "Notion 주소", 12, 126, 84)
        self.reference = self._field(102, 122, 320, "원본 표 주소 또는 데이터 소스 ID")
        self._inspect = button(
            self._header, "표 불러오기", 430, 122, 108, self._actions, "inspect:"
        )
        label(self._header, "수집할 표", 12, 92, 84)
        self.schemas = self._popup(self._header, 102, 88, 436, ["칼럼 조회 후 표시"], "schema:")
        label(self._header, "소스 이름", 12, 58, 84)
        self.name = self._field(102, 54, 436, "예: 팀 스프린트")
        label(self._header, "수집 방식", 12, 24, 84)
        self.mode = self._popup(self._header, 102, 20, 436, ["목록", "일정"], "mode:")
        self.mode.setToolTip_("일정은 시작·종료 날짜를 연결하고 목록은 기한을 선택할 수 있습니다.")
        label(self._header, "카테고리 *", 12, 160, 84)
        self.category = self._popup(self._header, 102, 156, 320, ["카테고리 선택"], "category:")
        self._manage = button(
            self._header, "카테고리 관리", 430, 156, 108, self._actions, "manageCategories:"
        )

        self._detail = NSView.alloc().initWithFrame_(frame(16, 0, 552, COLLAPSED_OPTIONS))
        self.document.addSubview_(self._detail)
        self._detail_card = card(self._detail, 0, 0, 552, COLLAPSED_OPTIONS)
        self._detail_title = label(self._detail, "세부 옵션", 12, 13, 320, bold=True)
        self._expand_options = button(
            self._detail, "펼치기", 430, 9, 108, self._actions, "options:"
        )
        self._advanced = NSView.alloc().initWithFrame_(
            frame(0, 0, 552, OPTION_HEIGHT - COLLAPSED_OPTIONS)
        )
        self._detail.addSubview_(self._advanced)
        label(self._advanced, "열기 주소", 12, 76, 84)
        self.url = self._field(102, 73, 436, "선택 사항", parent=self._advanced)
        label(self._advanced, "항목 정렬", 12, 41, 88, secondary=True)
        self.sort = self._popup(
            self._advanced, 102, 37, 170, ["정렬 없음", "날짜순", "제목순"], "changed:"
        )
        self.rounds = self._check("차시별 하위 표", 310, 36, 215, "structure:")
        self._remove = button(self._advanced, "소스 제거", 425, 5, 113, self._actions, "remove:")
        self._round_title: NSPopUpButton | None = None
        self._fill({})

    @property
    def busy(self) -> bool:
        """저장·탭 조작과 칼럼 조회가 겹치지 않게 한다."""
        return self._pending is not None

    @property
    def message(self) -> str:
        """실패는 별도로 안내하고 자동 재전송하지 않는다."""
        if self.busy:
            return "Notion 칼럼 조회 중…"
        if self._remove_pending and self.editor.editable and not self.error:
            return "저장하면 이 소스의 수집을 중단합니다."
        if self.editor.editable and not self._categories:
            return "‘카테고리 관리’에서 카테고리를 먼저 추가하세요."
        return self.error or self.editor.message

    def _popup(
        self, view: NSView, x: float, y: float, width: float, titles: list[str], action: str
    ) -> NSPopUpButton:
        """Cocoa 기본 목록으로 이름과 타입을 표시한다."""
        control = NSPopUpButton.alloc().initWithFrame_pullsDown_(frame(x, y, width, 26), False)
        set_titles(control, titles)
        control.setTarget_(self._actions)
        control.setAction_(action)
        view.addSubview_(control)
        return control

    def _field(
        self, x: float, y: float, width: float, placeholder: str, *, parent: NSView | None = None
    ) -> NSTextField:
        """한 줄의 입력은 저장하기 전까지 화면에 남긴다."""
        control = NSTextField.textFieldWithString_("")
        control.setFrame_(frame(x, y, width, 24))
        control.setPlaceholderString_(placeholder)
        control.setDelegate_(self._actions)
        (parent if parent is not None else self._header).addSubview_(control)
        return control

    def _check(
        self,
        title: str,
        x: float,
        y: float,
        width: float,
        action: str,
        *,
        parent: NSView | None = None,
    ) -> NSButton:
        """표마다 필요한 동작을 독립적으로 선택한다."""
        control = NSButton.checkboxWithTitle_target_action_(title, self._actions, action)
        control.setFrame_(frame(x, y, width, 24))
        (parent if parent is not None else self._advanced).addSubview_(control)
        return control

    def _mode(self) -> SourceKind:
        """카테고리 이름과 별개로 사용자가 선택한 수집 방식이다."""
        return COLLECTION_MODES[self.mode.indexOfSelectedItem()]

    def _uses_rounds(self) -> bool:
        """차시별 하위 표는 개별 기록을 읽는 목록에서 사용한다."""
        return self._mode() == "task" and self.rounds.state() == 1

    def mark_edited(self) -> None:
        """필드를 고치면 입력 오류를 지우고 저장 대기를 표시한다."""
        self.error = None
        if self._column_panel is None:
            self.editor.edited()

    def _section(self) -> str:
        """기존 QA 필터는 같은 목록 소스에서 계속 사용한다."""
        return "qa" if self._mode() == "task" and self._original.get("section") == "qa" else "tasks"

    def _fill(self, raw: dict[str, object]) -> None:
        """저장된 건 또는 새 소스의 기본 입력으로 바꾼다."""
        self._original = raw
        self.error = None
        self._profiles = ()
        self._schema = None
        self._lookup_reference = ""
        self._remove_pending = False
        self._open_after_inspect = False
        self._category_links = category_bindings(raw)
        self.name.setStringValue_(str(raw.get("name", "")))
        self._load_categories(raw)
        self.reference.setStringValue_(str(raw.get("dataSourceId", "")))
        self.url.setStringValue_(str(raw.get("pageUrl", "")))
        mode = raw.get("kind")
        titles = ["목록", "일정"]
        if mode == "leave":
            titles.append("휴가 승인 조회")
        set_titles(self.mode, titles)
        self.mode.selectItemAtIndex_(1 if mode == "meeting" else 2 if mode == "leave" else 0)
        self.rounds.setState_(int("roundTitleProperty" in raw))
        sort = raw.get("sort", "default")
        self.sort.selectItemAtIndex_(
            SOURCE_SORTS.index(sort) if sort in SOURCE_SORTS and isinstance(sort, str) else 0
        )
        self._set_schema_choices()
        self._draw_mapping()

    def _load_categories(self, raw: dict[str, object]) -> None:
        """설정에서 등록한 카테고리 중 하나를 ID로 연결한다."""
        state = self.editor.state
        self._categories = state.categories if state is not None else ()
        set_titles(
            self.category,
            [
                "카테고리 선택" if self._categories else "등록된 카테고리 없음",
                *(item.name for item in self._categories),
            ],
        )
        current = raw.get("categoryId")
        selected = next(
            (index for index, item in enumerate(self._categories, 1) if item.id == current), 0
        )
        self.category.selectItemAtIndex_(selected)

    def selected_category(self) -> SourceCategory | None:
        """현재 선택한 카테고리를 식별자로 가져온다."""
        index = self.category.indexOfSelectedItem() - 1
        return self._categories[index] if 0 <= index < len(self._categories) else None

    def change_category(self) -> None:
        """카테고리가 달라지면 그 칼럼에 맞는 연결만 남긴다."""
        category = self.selected_category()
        if category is None or category.id != self._original.get("categoryId"):
            self._category_links = {}
        else:
            self._category_links = category_bindings(self._original)
        self.mark_edited()
        self._summary()

    def _assign_category(self, value: dict[str, object]) -> None:
        """이름을 복제하지 않고 선택한 카테고리 ID만 저장한다."""
        value.pop("categoryId", None)
        index = self.category.indexOfSelectedItem() - 1
        if not 0 <= index < len(self._categories):
            raise SourceSettingsError("수집 소스의 카테고리를 선택하세요.")
        value["categoryId"] = self._categories[index].id

    def new_source(self) -> None:
        """저장 대기 중인 소스가 있을 때는 초안을 덮어쓰지 않는다."""
        state = self.editor.state
        if state is not None and self.editor.editable and not self.editor.dirty and not self.busy:
            self.list.selectItemAtIndex_(len(state.settings.sources))
            self.select()
            self.render()

    def toggle_options(self) -> None:
        """매핑은 보존하고 세부 옵션 카드만 펼치거나 접는다."""
        self._options_open = not self._options_open
        self._layout(self._body_height)
        if self._options_open:
            self.document.scrollPoint_((0, 0))

    def _set_schema_choices(self) -> None:
        """여러 원천 표는 명시적으로 하나를 고르게 한다."""
        titles = (
            ["표 선택", *(item.name for item in self._profiles)]
            if self._profiles
            else ["연결된 표" if self._original else "칼럼 조회 후 표시"]
        )
        set_titles(self.schemas, titles)

    def select(self) -> None:
        """저장 또는 다시 불러오기 전에는 다른 소스로 이동하지 않는다."""
        state = self.editor.state
        index = self.list.indexOfSelectedItem()
        if state is None or self.editor.dirty or self.busy:
            self.list.selectItemAtIndex_(self._selected)
            return
        values = state.settings.sources
        if 0 <= index <= len(values):
            self._selected = index
            self._fill(values[index] if index < len(values) else {})

    def change_structure(self) -> None:
        """직접 표와 차시 하위 표는 다른 스키마를 조회한다."""
        self._profiles = ()
        self._schema = None
        self._lookup_reference = ""
        self.error = None
        self._category_links.clear()
        self.editor.edited()
        self._set_schema_choices()
        self._draw_mapping()

    def change_mode(self) -> None:
        """조회한 표는 유지하고 새 수집 방식에 맞는 연결만 그린다."""
        supports_rounds = self._mode() == "task"
        self.rounds.setHidden_(not supports_rounds)
        if not supports_rounds and self.rounds.state() == 1:
            self.rounds.setState_(0)
            # 하위 표 속성은 이름으로 저장하므로 직접 표의 속성과 섞을 수 없다.
            self.change_structure()
            return
        selected = self._selected_columns()
        self._choices.clear()
        self.mark_edited()
        self._draw({key: value for key, value in selected.items() if key == "title"})

    def toggle_removal(self) -> None:
        """제거 요청은 다른 편집처럼 저장한 뒤에만 적용한다."""
        if self._original and self.editor.editable and not self.busy:
            self._remove_pending = not self._remove_pending
            self.error = None
            self.editor.edited()

    def inspect(self) -> None:
        """주소가 있어야 사용자 요청으로 읽기 작업을 제출한다."""
        if self.busy or not self.editor.editable:
            return
        reference = self.reference.stringValue().strip()
        if not reference:
            self.error = "원본 표 주소 또는 데이터 소스 ID를 입력하세요."
            return
        self.error = None
        self._pending_reference = reference
        self._pending = self._executor.submit(
            self._client.inspect, "notion", reference, rounds=self._uses_rounds()
        )

    def poll(self) -> None:
        """완료한 응답만 현재 페이지 상태에 반영한다."""
        if self._pending is None or not self._pending.done():
            return
        result, self._pending = self._pending, None
        try:
            profiles = result.result()
        except Exception as error:
            self._open_after_inspect = False
            self.error = (
                str(error)
                if isinstance(error, SourceSettingsError)
                else "칼럼 조회에 실패했습니다."
            )
            return
        self._lookup_reference = self._pending_reference
        prior_id = self._schema.id if self._schema else self._original.get("dataSourceId")
        self._profiles = profiles
        self._set_schema_choices()
        match = next((i for i, schema in enumerate(profiles, 1) if schema.id == prior_id), 0)
        self.schemas.selectItemAtIndex_(1 if len(profiles) == 1 else match)
        self.choose_schema()

    def choose_schema(self) -> None:
        """선택 표와 기존에 연결된 필드를 대조한다."""
        index = self.schemas.indexOfSelectedItem() - 1
        schema = self._profiles[index] if 0 <= index < len(self._profiles) else None
        same_table = (
            self._schema is not None and schema is not None and self._schema.id == schema.id
        )
        roles = self._selected_columns() if same_table else {}
        options = self._selected_options() if same_table else {}
        previous_round = (
            self._round_title.indexOfSelectedItem()
            if same_table and self._round_title is not None
            else None
        )
        self._schema = schema
        self.error = None
        if not same_table:
            original = self._original.get("dataSourceId")
            category = self.selected_category()
            self._category_links = (
                category_bindings(self._original)
                if schema is not None
                and schema.id == original
                and category is not None
                and category.id == self._original.get("categoryId")
                else {}
            )
        if self._schema is not None:
            if self._schema.id != self._original.get("dataSourceId"):
                self.editor.edited()
            if not self._original and not self.name.stringValue().strip():
                self.name.setStringValue_(self._schema.name[:120])
            reference = self.reference.stringValue().strip()
            if reference.startswith("https://") and not self.url.stringValue().strip():
                self.url.setStringValue_(reference)
        if same_table:
            self._choices = options
            self._draw(roles)
            if self._round_title is not None and previous_round is not None:
                self._round_title.selectItemAtIndex_(previous_round)
        else:
            self._draw_mapping()
        if self._schema is not None and self._open_after_inspect:
            self._open_after_inspect = False
            self.open_column_setup()

    def _column_value(self, column: SourceColumn) -> str:
        """일반 표는 ID, 차시별 하위 표는 공통 이름으로 매핑한다."""
        return column.name if self._uses_rounds() else column.id

    def _chosen(self, key: str) -> SourceColumn | None:
        """선택하지 않은 역할은 None으로 둔다."""
        row = self._mapping.get(key)
        if row is None:
            return None
        popup, columns = row
        index = popup.indexOfSelectedItem() - 1
        return columns[index] if 0 <= index < len(columns) else None

    def _selected_columns(self) -> dict[str, str]:
        """역할마다 실제로 선택한 값만 모은다."""
        original = self._original.get("properties")
        same_source = self._schema is not None and self._schema.id == self._original.get(
            "dataSourceId"
        )
        result: dict[str, str] = {}
        for key in self._mapping:
            column = self._chosen(key)
            if column is None:
                continue
            previous = original.get(key) if same_source and isinstance(original, dict) else None
            result[key] = (
                previous
                if isinstance(previous, str) and self._same_value(column, previous)
                else self._column_value(column)
            )
        return result

    def _default_choices(self) -> dict[str, int]:
        """이미 저장된 상태와 일정 분류를 같은 이름으로 복원한다."""
        kind = self._mode()
        if kind == "meeting":
            rules = self._original.get("eventTypes", {})
            if isinstance(rules, dict):
                return {
                    name: EVENT_CHOICES.index(value)
                    for name, value in rules.items()
                    if value in EVENT_CHOICES
                }
        values = self._original.get("approvedValues" if kind == "leave" else "completedStatuses")
        return {str(name): 1 for name in values} if isinstance(values, list) else {}

    def _draw_mapping(self) -> None:
        """기존 매핑에 맞는 칼럼이나 조회 안내를 표시한다."""
        raw = self._original.get("properties")
        selected: dict[str, str] = (
            {str(key): str(value) for key, value in raw.items()} if isinstance(raw, dict) else {}
        )
        if self._schema is not None and self._mode() != "leave" and "title" not in selected:
            if title := next(
                (column for column in self._schema.columns if column.kind == "title"), None
            ):
                selected["title"] = self._column_value(title)
        self._choices = self._default_choices()
        self._draw(selected)

    def _draw(self, selected: dict[str, str], *, preserve_scroll: bool = False) -> None:
        """칼럼 종류와 상태 행 수에 맞춰 카드 높이를 계산한다."""
        offset = 0.0
        if preserve_scroll:
            old_height = self.document.frame()[1][1]
            origin = self._page.contentView().bounds()[0][1]
            offset = max(0.0, old_height - self._viewport - origin)
        if self._body is not None:
            self._body.setHidden_(True)
            self.document.setSubviews_([self._header, self._detail])
        self._mapping.clear()
        self._options.clear()
        self._round_title = None
        self._summary(offset)
        schema = self._schema
        if schema is None:
            self._role_body = None
            return
        body = NSView.alloc().initWithFrame_(frame(0, 0, 628, 100))
        self._role_body = body
        kind = self._mode()
        roles = tuple(
            role
            for role in ROLES[kind]
            if role.key in selected or any(column.kind in role.types for column in schema.columns)
        )
        rule_key = {"task": "status", "leave": "approval", "meeting": "category"}[kind]
        options_column = next(
            (col for col in schema.columns if self._same_value(col, selected.get(rule_key, ""))),
            None,
        )
        options = options_column.options if options_column else ()
        height = 48 + 32 * (len(roles) + bool(schema.round_titles) + len(options))
        card(body, 0, 0, 628, height)
        label(body, "원본 칼럼 선택", 12, height - 32, 360, bold=True)
        y = height - 63
        if schema.round_titles:
            label(body, "차시 제목", 12, y + 4, 108)
            self._round_title = self._popup(
                body, 160, y, 446, list(schema.round_titles), "changed:"
            )
            saved = self._original.get("roundTitleProperty")
            self._round_title.selectItemAtIndex_(
                schema.round_titles.index(str(saved)) if saved in schema.round_titles else 0
            )
            y -= 32
        for role in roles:
            self._draw_role(body, role, schema, selected, y)
            y -= 32
        for name in options:
            if kind == "meeting":
                label(body, name, 12, y + 4, 338)
                control = self._popup(body, 365, y, 173, ["기타", "회의", "휴가"], "rules:")
                control.selectItemAtIndex_(self._choices.get(name, 0))
                self._options[name] = control
            else:
                prefix = "완료" if kind == "task" else "승인"
                check = NSButton.checkboxWithTitle_target_action_(
                    f"{prefix} · {name}", self._actions, "changed:"
                )
                check.setFrame_(frame(12, y, 526, 26))
                check.setState_(self._choices.get(name, 0))
                body.addSubview_(check)
                self._options[name] = check
            y -= 32
        body.setFrameSize_((628, height))
        self._update_sheet()

    def _summary(self, offset: float = 0.0) -> None:
        """메인 폼에는 편집기 대신 선택 상태와 열기 버튼을 둔다."""
        if self._body is not None:
            self.document.setSubviews_([self._header, self._detail])
        height = 82
        body = NSView.alloc().initWithFrame_(frame(16, 0, 552, height))
        self._body = body
        card(body, 0, 0, 552, height)
        label(body, "원본 칼럼 연결", 12, 48, 390, bold=True)
        selected = self.selected_category()
        config = selected.configuration if selected is not None else None
        message = (
            "카테고리를 선택한 뒤 표를 연결하세요."
            if selected is None
            else "저장 칼럼은 카테고리 설정에서 추가할 수 있습니다."
            if config is None or not config.columns
            else f"카테고리 칼럼 {len(config.columns)}개 중 {len(self._category_links)}개 연결"
        )
        label(body, message, 12, 21, 420, secondary=True)
        self._setup_columns = button(
            body, "연결 편집", 430, 42, 108, self._actions, "setupColumns:"
        )
        self.document.addSubview_(body)
        self._layout(height, offset)

    def _update_sheet(self) -> None:
        """원본 칼럼의 기본 연결을 시트에서 편집한다."""
        if self._column_panel is not None and self._role_body is not None:
            self._column_panel.update_behavior(self._role_body, self._role_body.frame()[1][1])

    def open_column_setup(self) -> None:
        """등록 카테고리의 칼럼과 실제 원본을 연결하는 시트를 연다."""
        if self.busy or not self.editor.editable or self._column_panel is not None:
            return
        category = self.selected_category()
        if category is None:
            self.error = "카테고리를 먼저 선택하세요."
            return
        schema = self._schema
        if schema is None:
            self._open_after_inspect = True
            self.inspect()
            return
        window = self.document.window()
        if window is None:
            return
        previous_roles = self._selected_columns()
        previous_options = self._selected_options()
        previous_round = (
            self._round_title.indexOfSelectedItem() if self._round_title is not None else None
        )

        def closed(applied: bool) -> None:
            """취소하면 시트를 열기 직전의 원본 연결을 복원한다."""
            self._column_panel = None
            if applied:
                self.mark_edited()
            else:
                self._choices = previous_options
                self._draw(previous_roles, preserve_scroll=True)
                if self._round_title is not None and previous_round is not None:
                    self._round_title.selectItemAtIndex_(previous_round)
            self._summary()
            self.render()

        choices = tuple(
            SourceColumn(self._column_value(column), column.name, column.kind, column.options)
            for column in schema.columns
        )
        columns = category.configuration.columns if category.configuration is not None else ()
        self._column_panel = ColumnSetupSheet(
            window,
            self.name.stringValue().strip() or schema.name,
            category.name,
            columns,
            choices,
            self._category_links,
            self._apply_columns,
            closed,
            rounds=self._uses_rounds(),
        )
        self._update_sheet()
        self.render()
        self._column_panel.show()

    def _apply_columns(self, selected: dict[str, ColumnBinding]) -> str | None:
        """원본 연결을 검증한 뒤 소스 저장 초안에 반영한다."""
        try:
            self._validate_roles(self._selected_columns())
        except SourceSettingsError as error:
            return str(error)
        if self._mode() == "leave" and 1 not in self._selected_options().values():
            return "승인된 휴가에 쓰는 상태를 하나 이상 선택하세요."
        self._category_links = selected
        return None

    def _validate_roles(self, values: dict[str, str]) -> None:
        """켜 둔 동작에 필요한 칼럼만 요구한다."""
        optional = {"assignees", "participants", "status"}
        for role in ROLES[self._mode()]:
            if role.required and role.key not in optional and role.key not in values:
                raise SourceSettingsError(f"'{role.title}'에 사용할 원본 칼럼을 선택하세요.")

    def _same_value(self, column: SourceColumn, value: str) -> bool:
        """이전 Notion 속성 ID의 인코딩 표기도 함께 인식한다."""
        candidate = self._column_value(column)
        return candidate == value if self._uses_rounds() else unquote(candidate) == unquote(value)

    def _draw_role(
        self,
        body: NSView,
        role: ColumnRole,
        schema: SourceSchema,
        selected: dict[str, str],
        y: float,
    ) -> None:
        """역할에 맞는 타입만 선택 목록에 넣는다."""
        columns = tuple(col for col in schema.columns if col.kind in role.types)
        required = role.required and role.key not in {"assignees", "participants", "status"}
        person = role.key in {"assignees", "participants"}
        label(body, role.title + (" *" if required else ""), 12, y + 4, 144)
        popup = self._popup(
            body,
            160,
            y,
            446,
            [
                "선택" if required else "사용 안 함 · 모든 항목" if person else "사용 안 함",
                *(col.name for col in columns),
            ],
            "columns:",
        )
        popup.selectItemAtIndex_(
            next(
                (
                    index
                    for index, column in enumerate(columns, 1)
                    if self._same_value(column, selected.get(role.key, ""))
                ),
                0,
            )
        )
        self._mapping[role.key] = popup, columns

    def change_columns(self) -> None:
        """바뀐 역할에 따라 연결된 상태 옵션만 다시 계산한다."""
        self.mark_edited()
        round_index = self._round_title.indexOfSelectedItem() if self._round_title else None
        selected = self._selected_columns()
        self._choices = self._selected_options()
        self._draw(selected, preserve_scroll=True)
        if self._round_title is not None and round_index is not None:
            self._round_title.selectItemAtIndex_(round_index)
        if self._column_panel is not None:
            self.render()

    def _selected_options(self) -> dict[str, int]:
        """할당된 값만 추려 소스의 상태 규칙에 사용한다."""
        return {
            name: control.indexOfSelectedItem()
            if isinstance(control, NSPopUpButton)
            else control.state()
            for name, control in self._options.items()
        }

    def _layout(self, body_height: float, offset: float = 0.0) -> None:
        """편집 가능한 전체 높이를 스크롤 페이지에 반영한다."""
        self._body_height = body_height
        detail_height = OPTION_HEIGHT if self._options_open else COLLAPSED_OPTIONS
        height = max(self._viewport, body_height + HEADER_HEIGHT + detail_height + 40)
        self.document.setFrameSize_((584, height))
        self._header.setFrame_(frame(16, height - HEADER_HEIGHT - 8, 552, HEADER_HEIGHT))
        body_bottom = height - body_height - HEADER_HEIGHT - 20
        if self._body is not None:
            self._body.setFrame_(frame(16, body_bottom, 552, body_height))
        self._detail.setFrame_(frame(16, body_bottom - detail_height - 12, 552, detail_height))
        self._detail_card.setFrame_(frame(0, 0, 552, detail_height))
        self._detail_title.setFrame_(frame(12, detail_height - 32, 320, 22))
        self._expand_options.setFrame_(frame(430, detail_height - 35, 108, 26))
        self._expand_options.setTitle_("접기" if self._options_open else "펼치기")
        self._advanced.setHidden_(not self._options_open)
        self.document.scrollPoint_((0, max(0, height - self._viewport - offset)))

    def draft(self) -> SourceDraft:
        """수집 타입과 필수 매핑을 검사하고 선택한 소스만 바꾼다."""
        state = self.editor.state
        if state is None:
            raise SourceSettingsError("수집 설정을 다시 불러오세요.")
        sources = list(state.settings.sources)
        if self._remove_pending:
            sources.pop(self._selected)
            return SourceDraft(state.settings.revision, tuple(sources))
        assignment: dict[str, object] = {}
        self._assign_category(assignment)
        schema = self._schema
        reference = self.reference.stringValue().strip()
        if (schema is not None and reference != self._lookup_reference) or (
            schema is None and reference != self._original.get("dataSourceId", "")
        ):
            raise SourceSettingsError("바뀐 주소로 칼럼을 조회하고 수집할 표를 선택하세요.")
        kind, section = self._mode(), self._section()
        if schema is None:
            if (
                not self._original
                or self._original.get("kind") != kind
                or (self._uses_rounds() != ("roundTitleProperty" in self._original))
            ):
                raise SourceSettingsError("Notion 칼럼을 조회하고 수집할 표를 선택하세요.")
            value = dict(self._original)
        else:
            values = self._selected_columns()
            self._validate_roles(values)
            value = {
                "provider": "notion",
                "kind": kind,
                "dataSourceId": schema.id,
                "properties": values,
                "columnLabels": {
                    key: column.name[:250]
                    for key in values
                    if (column := self._chosen(key)) is not None
                },
            }
            if section == "qa":
                value["section"] = section
            if self._round_title is not None:
                value["roundTitleProperty"] = schema.round_titles[
                    self._round_title.indexOfSelectedItem()
                ]
            old = self._original
            stays_legacy = (
                old.get("kind") == kind
                and old.get("flexible") is not True
                and all(role.key in values for role in ROLES[kind] if role.required)
                and (self._round_title is None or section == "qa")
            )
            if not stays_legacy:
                value["flexible"] = True
                if kind == "meeting":
                    if self._original.get("defaultEventType") in ("meeting", "leave", "other"):
                        value["defaultEventType"] = self._original["defaultEventType"]
            choices = self._selected_options()
            if kind == "meeting" and "category" in values:
                value["eventTypes"] = {
                    name: EVENT_CHOICES[index] for name, index in choices.items() if index
                }
            if kind in {"task", "leave"}:
                selected = [name for name, index in choices.items() if index == 1]
                if kind == "leave" and not selected:
                    raise SourceSettingsError("승인된 휴가에 쓰는 상태를 하나 이상 선택하세요.")
                if kind == "leave" or selected or "completedStatuses" in self._original:
                    value["approvedValues" if kind == "leave" else "completedStatuses"] = selected
        value.pop("columnMappings", None)
        if self._category_links:
            value["columnMappings"] = column_bindings_payload(self._category_links)
        name = self.name.stringValue().strip()
        if len(name) > 120:
            raise SourceSettingsError("이름은 120자 이내로 입력하세요.")
        value.pop("name", None)
        if name:
            value["name"] = name
        value.update(assignment)
        try:
            url = page_url(self.url.stringValue())
        except PageLinksError:
            raise SourceSettingsError("열려는 Notion 페이지의 주소를 입력하세요.") from None
        value.pop("pageUrl", None)
        if url:
            value["pageUrl"] = url
        value.pop("sort", None)
        sort = SOURCE_SORTS[self.sort.indexOfSelectedItem()]
        if sort != "default":
            value["sort"] = sort
        if self._selected == len(sources):
            sources.append(value)
        else:
            sources[self._selected] = value
        return SourceDraft(state.settings.revision, tuple(sources))

    def render(self) -> None:
        """확정 조회·저장 때만 폼을 재구성하고 나머지 입력을 보존한다."""
        self.poll()
        state = self.editor.state
        if state is not None and self._generation != self.editor.generation:
            self._generation = self.editor.generation
            sources = state.settings.sources
            self._selected = min(self._selected, len(sources))
            set_titles(
                self.list,
                [source_menu_name(raw, i + 1) for i, raw in enumerate(sources)] + ["새 소스"],
            )
            self.list.selectItemAtIndex_(self._selected)
            self._fill(sources[self._selected] if self._selected < len(sources) else {})
        editable = self.editor.editable and not self.busy
        self.list.setEnabled_(editable and not self.editor.dirty)
        self._remove.setTitle_("제거 취소" if self._remove_pending else "소스 제거")
        self._remove.setEnabled_(editable and bool(self._original))
        enabled = editable and not self._remove_pending
        self._inspect.setEnabled_(enabled)
        self.schemas.setEnabled_(enabled and bool(self._profiles))
        self.rounds.setHidden_(self._mode() != "task")
        for control in (
            self.name,
            self.category,
            self.reference,
            self.url,
            self.mode,
            self.rounds,
            self.sort,
            *self._options.values(),
        ):
            control.setEnabled_(enabled)
        self._new.setEnabled_(editable and not self.editor.dirty)
        self._manage.setEnabled_(editable and not self.editor.dirty)
        if not self._categories:
            for control in (self.name, self.reference, self._inspect, self.category, self.mode):
                control.setEnabled_(False)
        self._setup_columns.setEnabled_(
            enabled
            and self.selected_category() is not None
            and bool(self.reference.stringValue().strip())
            and self._column_panel is None
        )
        for menu, _ in self._mapping.values():
            menu.setEnabled_(enabled)
        if self._round_title is not None:
            self._round_title.setEnabled_(enabled)

    def close(self) -> None:
        """설정 창을 종료할 때 자동으로 외부 조회를 반복하지 않는다."""
        self._executor.shutdown(wait=False, cancel_futures=True)
