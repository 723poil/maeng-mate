"""수집 소스를 선택하고 실제 칼럼에 앱 필드를 연결하는 Cocoa 폼이다."""

from collections import Counter
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import replace
from uuid import uuid4

from AppKit import (
    NSBox,
    NSButton,
    NSColor,
    NSMenu,
    NSMenuItem,
    NSPopUpButton,
    NSScrollView,
    NSTextAlignmentRight,
    NSTextField,
    NSView,
)
from Foundation import NSObject

from src.application.category_settings_codec import category_bindings
from src.application.source_edit import (
    EVENT_CHOICES,
    SourceEdit,
    TableEdit,
    matches_property,
    selected_properties,
    source_draft,
    uses_saved_layout,
    validate_roles,
)
from src.application.source_settings import (
    CollectionDraft,
    SourceColumn,
    SourceDraft,
    SourceSchema,
    SourceSettingsClient,
    SourceSettingsError,
    SourceSettingsState,
    SourceTable,
)
from src.domain.category_configuration import ColumnBinding
from src.domain.display_columns import DISPLAY_COLUMN_TYPES
from src.domain.models import SourceKind
from src.domain.source_display import SOURCE_SORTS, SourceCategory
from src.domain.source_roles import ROLE_TEMPLATES, ColumnRole, source_roles
from src.domain.work_dates import canonical_identity
from src.presentation.column_setup_sheet import ColumnSetupSheet
from src.presentation.native_views import (
    BUTTON_BEZEL_INSET,
    CARD_GAP,
    CARD_PADDING,
    CARD_TITLE,
    CARD_WIDTH,
    FORM_CONTROL_X,
    FORM_LABEL_WIDTH,
    FORM_ROW,
    FORM_ROW_GAP,
    button,
    card,
    card_height,
    form_control_y,
    form_divider,
    form_dividers,
    form_row_y,
    frame,
    label,
    place_label,
)
from src.presentation.runtime_settings_controller import SettingsEditor

type SourceEditor = SettingsEditor[CollectionDraft, SourceSettingsState]
# 연결 카드는 Notion 표·표 선택·이름·표 종류·카테고리 다섯 줄이다.
HEADER_HEIGHT = card_height(5)
# 연결한 표 목록 카드에서 한 줄의 높이다.
SOURCE_ROW = FORM_ROW
# 새 표에서 종류가 맞는 첫 칼럼을 미리 골라 둘 역할이다. 사람 칸은 본인 ID 설정이 필요해 고르지 않는다.
AUTO_ROLES = frozenset({"title", "status", "dueDate", "period"})
# 고급 카드를 접었을 때는 제목 줄만 남는다.
COLLAPSED_OPTIONS = CARD_PADDING + CARD_TITLE + CARD_PADDING
PAGE_TOP = 8.0
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
    name = str(raw.get("name") or f"표 {position}")
    return name if raw.get("categoryId") else f"{name} · 카테고리 선택 필요"


def kind_title(kind: object) -> str:
    """수집 방식을 사용자가 아는 표 종류 이름으로 바꾼다."""
    return "일정" if kind == "meeting" else "휴가 승인" if kind == "leave" else "할 일"


def recommended_mode(schema: SourceSchema) -> int:
    """칸 종류로 표 종류를 추천한다. 상태 칸이 있으면 할 일, 날짜만 있으면 일정이다."""
    kinds = {column.kind for column in schema.columns}
    if "status" in kinds:
        return 0
    return 1 if "date" in kinds else 0


class SourceFormActions(NSObject):
    """사용자가 누른 소스·조회·매핑 조작을 전달한다."""

    owner: "SourceSettingsForm"

    def pickSource_(self, sender: NSButton) -> None:
        """목록에서 누른 연결을 편집 대상으로 고른다."""
        self.owner.pick_source(sender.tag())

    def findTables_(self, sender: object) -> None:
        """Notion에서 연결할 수 있는 표 목록을 읽는다."""
        self.owner.find_tables()

    def chooseTable_(self, sender: NSMenuItem) -> None:
        """표 목록 메뉴에서 고른 표로 주소를 채우고 바로 칸을 읽는다."""
        self.owner.choose_table(sender.tag())

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

    def addRole_(self, sender: object) -> None:
        """선택한 원본 타입의 사용자 역할을 추가한다."""
        self.owner.add_role()

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
        self._role_names: dict[str, NSTextField] = {}
        self._roles = ROLE_TEMPLATES["task"]
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
        self._tables_pending: Future[tuple[SourceTable, ...]] | None = None
        self._tables: tuple[SourceTable, ...] = ()
        # 연결한 표를 한 줄씩 보여 주는 목록 카드다. 줄은 상태가 바뀔 때 다시 그린다.
        self._sources_view = NSView.alloc().initWithFrame_(frame(16, 0, CARD_WIDTH, 0))
        self.document.addSubview_(self._sources_view)
        self._source_rows: list[NSButton] = []

        # 연결 카드는 지금 필요한 줄만 보인다. 위치는 `_place_header`가 줄 구성에 맞춰 정한다.
        self._header = NSView.alloc().initWithFrame_(frame(16, 0, CARD_WIDTH, HEADER_HEIGHT))
        self.document.addSubview_(self._header)
        self._header_card = card(self._header, 0, 0, CARD_WIDTH, HEADER_HEIGHT)
        self._header_title = label(self._header, "새 연결", 0, 0, 300, size=13, bold=True)
        self._header_height = HEADER_HEIGHT
        self._header_dividers: list[NSBox] = []
        self._manual = False
        # 선택 모델은 기존처럼 팝업 버튼에 두고 화면에는 목록 카드만 보인다.
        self.list = self._popup(self._header, 0, 0, 10, ["새 소스"], "select:")
        self.list.setHidden_(True)
        self._header_labels = {
            key: label(self._header, text, 0, 0, FORM_LABEL_WIDTH - 8)
            for key, text in (
                ("table", "Notion 표"),
                ("schema", "표 선택"),
                ("name", "이름"),
                ("mode", "표 종류"),
                ("category", "카테고리"),
            )
        }
        # 표는 목록에서 고른다. 주소·ID 입력칸은 메뉴의 ‘주소로 입력…’을 골랐을 때만 보인다.
        self._find = button(self._header, "표 고르기…", 0, 0, 240, self._actions, "findTables:")
        self.reference = self._field(0, 0, 300, "표 주소 또는 ID")
        self._inspect = button(self._header, "불러오기", 0, 0, 90, self._actions, "inspect:")
        self.schemas = self._popup(self._header, 0, 0, 300, ["불러온 뒤 선택"], "schema:")
        self.name = self._field(0, 0, 300, "예: 팀 스프린트")
        self.mode = self._popup(self._header, 0, 0, 200, ["할 일", "일정"], "mode:")
        self.mode.setToolTip_("할 일은 담당자·상태·마감일을, 일정은 시작·종료 날짜를 씁니다.")
        self.category = self._popup(self._header, 0, 0, 240, ["카테고리 선택"], "category:")
        # 카테고리 관리는 사이드바의 ‘카테고리’ 페이지가 맡아 이 카드에서는 버튼을 두지 않는다.
        self._manage = button(self._header, "", 0, 0, 1, self._actions, "manageCategories:")
        self._manage.setHidden_(True)

        # 고급 카드: 제목 줄만 보이다가 펼치면 원본 링크·정렬·하위 표·(부모)·연결 해제 줄이 나온다.
        self._detail = NSView.alloc().initWithFrame_(frame(16, 0, CARD_WIDTH, COLLAPSED_OPTIONS))
        self.document.addSubview_(self._detail)
        self._detail_card = card(self._detail, 0, 0, CARD_WIDTH, COLLAPSED_OPTIONS)
        self._detail_title = label(self._detail, "고급", 0, 0, 300, size=13, bold=True)
        self._expand_options = button(self._detail, "펼치기", 0, 0, 80, self._actions, "options:")
        self._advanced = NSView.alloc().initWithFrame_(frame(0, 0, CARD_WIDTH, 0))
        self._detail.addSubview_(self._advanced)
        # 예전 구조의 묶음 뷰는 남겨 두되 고급 카드 전체 좌표를 그대로 쓴다.
        self._common_options = self._advanced
        self._detail_labels = {
            key: label(self._advanced, text, 0, 0, FORM_LABEL_WIDTH - 8)
            for key, text in (
                ("url", "원본 링크"),
                ("sort", "정렬"),
                ("rounds", "하위 표"),
                ("parent", "부모 항목"),
            )
        }
        self.url = self._field(0, 0, 300, "‘원본 ↗’을 눌렀을 때 열 주소", parent=self._advanced)
        self.sort = self._popup(
            self._advanced, 0, 0, 220, ["정렬 없음", "날짜순", "제목순"], "changed:"
        )
        self._sort_keys = list(SOURCE_SORTS)
        self.sort_direction = self._popup(
            self._advanced, 0, 0, 150, ["오름차순", "내림차순"], "changed:"
        )
        self.rounds = self._check(
            "페이지 안의 하위 표를 모두 모으기", 0, 0, 300, "structure:", parent=self._advanced
        )
        self.parent_display = self._check("부모 제목 표시", 0, 0, 140, "changed:")
        self.parent_sort = self._popup(self._advanced, 0, 0, 230, ["부모 정렬 없음"], "changed:")
        self._parent_hint = label(
            self._advanced,
            "카드 정렬을 지정했다면 ‘부모 정렬값’을 추가하세요.",
            0,
            0,
            CARD_WIDTH - FORM_CONTROL_X - CARD_PADDING,
            secondary=True,
        )
        self._parent_orders: list[tuple[str, str | None]] = []
        self._remove = button(self._advanced, "연결 해제", 0, 0, 100, self._actions, "remove:")
        self._detail_dividers: list[NSBox] = []
        self._round_title: NSPopUpButton | None = None
        self._draw_sources()
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

    def _header_rows(self) -> list[str]:
        """연결 카드에 보일 줄을 고른다. 표가 하나면 ‘표 선택’ 줄은 숨긴다."""
        return [
            "table",
            *(("schema",) if len(self._profiles) > 1 else ()),
            "name",
            "mode",
            "category",
        ]

    def _place_header(self) -> float:
        """보일 줄만 간격 체계대로 놓고 카드 높이를 돌려준다."""
        rows = self._header_rows()
        height = card_height(len(rows))
        self._header_height = height
        self._header_card.setFrame_(frame(0, 0, CARD_WIDTH, height))
        place_label(
            self._header_title, CARD_PADDING, height - CARD_PADDING - CARD_TITLE, 300, CARD_TITLE
        )
        y = {key: form_row_y(height, index) for index, key in enumerate(rows)}
        for key, name in self._header_labels.items():
            name.setHidden_(key not in y)
            if key in y:
                place_label(name, CARD_PADDING, y[key], FORM_LABEL_WIDTH - 8, FORM_ROW)
        row = y["table"]
        self._find.setHidden_(self._manual)
        self.reference.setHidden_(not self._manual)
        self._inspect.setHidden_(not self._manual)
        self._find.setFrame_(
            frame(FORM_CONTROL_X - BUTTON_BEZEL_INSET, form_control_y(row, 26), 240, 26)
        )
        self.reference.setFrame_(frame(FORM_CONTROL_X, form_control_y(row, 24), 300, 24))
        self._inspect.setFrame_(
            frame(
                CARD_WIDTH - CARD_PADDING - 90 + BUTTON_BEZEL_INSET,
                form_control_y(row, 26),
                90,
                26,
            )
        )
        self.schemas.setHidden_("schema" not in y)
        if "schema" in y:
            self.schemas.setFrame_(
                frame(FORM_CONTROL_X - 3, form_control_y(y["schema"], 26), 300, 26)
            )
        self.name.setFrame_(frame(FORM_CONTROL_X, form_control_y(y["name"], 24), 300, 24))
        self.mode.setFrame_(frame(FORM_CONTROL_X - 3, form_control_y(y["mode"], 26), 200, 26))
        self.category.setFrame_(
            frame(FORM_CONTROL_X - 3, form_control_y(y["category"], 26), 240, 26)
        )
        while len(self._header_dividers) < len(rows) - 1:
            self._header_dividers.append(form_divider(self._header, 0))
        for index, divider in enumerate(self._header_dividers):
            divider.setHidden_(index >= len(rows) - 1)
            if index < len(rows) - 1:
                divider.setFrame_(
                    frame(
                        CARD_PADDING,
                        form_row_y(height, index) - FORM_ROW_GAP / 2,
                        CARD_WIDTH - 2 * CARD_PADDING,
                        1,
                    )
                )
        return height

    def _row_label(self, text: str, y: float) -> None:
        """연결 카드의 항목 이름은 왼쪽 칸에서 입력 줄 가운데에 둔다."""
        name = label(self._header, text, 0, 0, FORM_LABEL_WIDTH - 8)
        place_label(name, CARD_PADDING, y, FORM_LABEL_WIDTH - 8, FORM_ROW)

    def _draw_sources(self) -> None:
        """연결한 표를 한 줄씩 그리고 지금 편집 중인 줄을 강조한다."""
        state = self.editor.state
        sources = state.settings.sources if state is not None else ()
        names = {category.id: category.name for category in self._categories}
        rows = [
            (
                source_menu_name(raw, index + 1),
                f"{names.get(str(raw.get('categoryId')), '카테고리 없음')} · {kind_title(raw.get('kind'))}",
            )
            for index, raw in enumerate(sources)
        ]
        # 새 연결은 아래 연결 카드가 보여 주므로 목록에 줄을 만들지 않는다.
        # 저장된 연결이 하나도 없으면 목록 카드 자체를 숨긴다.
        height = card_height(len(rows)) if rows else 0.0
        self._sources_view.setSubviews_([])
        self._sources_view.setFrameSize_((CARD_WIDTH, height))
        self._sources_view.setHidden_(not rows)
        card(self._sources_view, 0, 0, CARD_WIDTH, height)
        form_dividers(self._sources_view, height, len(rows))
        title = label(self._sources_view, "연결한 표", 0, 0, 300, size=13, bold=True)
        place_label(title, CARD_PADDING, height - CARD_PADDING - CARD_TITLE, 300, CARD_TITLE)
        self._new = button(
            self._sources_view,
            "표 연결",
            CARD_WIDTH - CARD_PADDING - 88 + BUTTON_BEZEL_INSET,
            height - CARD_PADDING - CARD_TITLE - 3,
            88,
            self._actions,
            "newSource:",
        )
        self._source_rows = []
        for index, (name, detail) in enumerate(rows):
            y = form_row_y(height, index)
            selected = index == self._selected
            if selected:
                # 선택한 줄은 위아래로 여유를 둔 옅은 바탕으로만 표시한다.
                mark = card(
                    self._sources_view,
                    CARD_PADDING / 2,
                    y + 6,
                    CARD_WIDTH - CARD_PADDING,
                    SOURCE_ROW - 12,
                    input_area=True,
                )
                mark.setFillColor_(NSColor.labelColor().colorWithAlphaComponent_(0.07))
                mark.setBorderWidth_(0)
            caption = label(self._sources_view, name, 0, 0, 260, size=13, bold=selected)
            place_label(caption, CARD_PADDING, y, 260, SOURCE_ROW)
            sub_caption = label(self._sources_view, detail, 0, 0, 240, size=12, secondary=True)
            place_label(sub_caption, CARD_WIDTH - CARD_PADDING - 240, y, 240, SOURCE_ROW)
            sub_caption.setAlignment_(NSTextAlignmentRight)
            # 줄 전체를 누를 수 있게 투명 버튼을 맨 위에 둔다.
            hit = button(
                self._sources_view,
                "",
                0,
                y,
                CARD_WIDTH,
                self._actions,
                "pickSource:",
                bordered=False,
            )
            hit.setFrame_(frame(0, y, CARD_WIDTH, SOURCE_ROW))
            hit.setTransparent_(True)
            hit.setTag_(index)
            self._source_rows.append(hit)

    def pick_source(self, index: int) -> None:
        """목록 줄을 누르면 숨긴 선택 모델을 거쳐 기존 선택 흐름을 그대로 쓴다."""
        if index == self._selected:
            return
        self.list.selectItemAtIndex_(index)
        self.select()
        self._draw_sources()
        self._layout(self._body_height)
        self.render()

    def find_tables(self) -> None:
        """사용자가 누를 때만 연동에 공유된 표 목록을 읽는다."""
        if self.busy or self._tables_pending is not None or not self.editor.editable:
            return
        self.error = None
        self._tables_pending = self._executor.submit(self._client.tables, "notion")

    def _show_tables(self) -> None:
        """읽은 표 목록을 ‘찾기…’ 버튼 아래 메뉴로 보여 준다."""
        menu = NSMenu.alloc().initWithTitle_("Notion 표")
        if not self._tables:
            empty = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
                "연동에 공유된 표가 없습니다", None, ""
            )
            empty.setEnabled_(False)
            menu.addItem_(empty)
        for index, table in enumerate(self._tables):
            item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
                table.title, "chooseTable:", ""
            )
            item.setTarget_(self._actions)
            item.setTag_(index)
            menu.addItem_(item)
        menu.addItem_(NSMenuItem.separatorItem())
        manual = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
            "주소로 입력…", "chooseTable:", ""
        )
        manual.setTarget_(self._actions)
        manual.setTag_(-1)
        menu.addItem_(manual)
        menu.popUpMenuPositioningItem_atLocation_inView_(None, (0, -4), self._find)

    def choose_table(self, index: int) -> None:
        """고른 표의 ID로 주소를 채우고 바로 칸을 불러온다. -1이면 주소 입력칸을 연다."""
        if index == -1:
            self._manual = True
            self._layout(self._body_height)
            window = self.reference.window()
            if window is not None:
                window.makeFirstResponder_(self.reference)
            return
        if not 0 <= index < len(self._tables):
            return
        table = self._tables[index]
        self._manual = False
        self._find.setTitle_(table.title[:40])
        self.reference.setStringValue_(table.id)
        if not self.name.stringValue().strip():
            self.name.setStringValue_(table.title[:120])
        self.mark_edited()
        self.inspect()

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
        # 저장을 다시 시도해도 같은 초안은 하나의 논리 소스로 식별한다.
        saved_id = raw.get("sourceId", raw.get("dataSourceId"))
        self._source_id = saved_id if isinstance(saved_id, str) else str(uuid4())
        saved_classification = raw.get("classification")
        self._classification: dict[str, object] = (
            dict(saved_classification) if isinstance(saved_classification, dict) else {}
        )
        self.error = None
        self._profiles = ()
        self._schema = None
        self._lookup_reference = ""
        self._remove_pending = False
        self._open_after_inspect = False
        self._tables = ()
        self._manual = False
        self._find.setTitle_("연결된 표" if raw else "표 고르기…")
        self._category_links = category_bindings(raw)
        self.name.setStringValue_(str(raw.get("name", "")))
        self._load_categories(raw)
        self.reference.setStringValue_(str(raw.get("dataSourceId", "")))
        self.url.setStringValue_(str(raw.get("pageUrl", "")))
        mode = raw.get("kind")
        titles = ["할 일", "일정"]
        if mode == "leave":
            titles.append("휴가 승인")
        set_titles(self.mode, titles)
        self.mode.selectItemAtIndex_(1 if mode == "meeting" else 2 if mode == "leave" else 0)
        self._roles = source_roles(self._mode(), raw.get("columnRoles"))
        self.rounds.setState_(int("parentTitleProperty" in raw or "roundTitleProperty" in raw))
        self._parent_orders = []
        sort = raw.get("sort", "default")
        self._refresh_sort(sort if isinstance(sort, str) else "default")
        self.sort_direction.selectItemAtIndex_(int(raw.get("sortDirection") == "desc"))
        self._set_schema_choices()
        self._draw_mapping()

    def _refresh_sort(self, selected: str | None = None) -> None:
        """선택한 카테고리에 실제 연결한 칼럼만 정렬 기준으로 제공한다."""
        if selected is None:
            selected = self._sort_keys[self.sort.indexOfSelectedItem()]
        category = self.selected_category()
        config = category.configuration if category else None
        columns = (
            [item for item in config.columns if item.id in self._category_links] if config else []
        )
        self._sort_keys = [*SOURCE_SORTS, *(f"column:{item.id}" for item in columns)]
        set_titles(self.sort, ["기본 정렬", "날짜순", "제목순", *(item.name for item in columns)])
        self.sort.selectItemAtIndex_(
            self._sort_keys.index(selected) if selected in self._sort_keys else 0
        )

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
        if (
            category is None
            or category.id != self._original.get("categoryId")
            or not self._uses_saved_layout()
        ):
            self._category_links = {}
        else:
            self._category_links = category_bindings(self._original)
        self.mark_edited()
        self._summary()

    def new_source(self) -> None:
        """저장 대기 중인 소스가 있을 때는 초안을 덮어쓰지 않는다."""
        state = self.editor.state
        if state is not None and self.editor.editable and not self.editor.dirty and not self.busy:
            self.list.selectItemAtIndex_(len(state.settings.sources))
            self.select()
            # 목록의 선택 표시를 지우고 연결 카드를 새 연결로 다시 배치한다.
            self._draw_sources()
            self._layout(self._body_height)
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
            else ["연결된 표" if self._original else "불러온 뒤 선택"]
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
        self._roles = ROLE_TEMPLATES[self._mode()]
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
        if self._tables_pending is not None and self._tables_pending.done():
            pending, self._tables_pending = self._tables_pending, None
            try:
                self._tables = pending.result()
                self._show_tables()
            except Exception as error:
                self.error = (
                    str(error)
                    if isinstance(error, SourceSettingsError)
                    else "표 목록을 읽지 못했습니다."
                )
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
        # 조회 결과로 표 목록을 교체하기 전에 현재 폼이 저장된 표를 편집 중인지 확인한다.
        first_saved_lookup = self._schema is None and self._uses_saved_layout()
        self._profiles = profiles
        self._set_schema_choices()
        match = next(
            (
                i
                for i, schema in enumerate(profiles, 1)
                if isinstance(prior_id, str)
                and canonical_identity(schema.id) == canonical_identity(prior_id)
            ),
            0,
        )
        self.schemas.selectItemAtIndex_(1 if len(profiles) == 1 else match)
        # 새 연결은 칸 종류를 보고 표 종류를 먼저 골라 둔다. 저장된 연결은 사용자의 선택을 유지한다.
        if not self._original and len(profiles) == 1 and self.mode.numberOfItems() >= 2:
            self.mode.selectItemAtIndex_(recommended_mode(profiles[0]))
            self._roles = ROLE_TEMPLATES[self._mode()]
        self.choose_schema(first_saved_lookup=first_saved_lookup)

    def choose_schema(self, *, first_saved_lookup: bool = False) -> None:
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
            saved_roles = (
                self._original.get("columnRoles")
                if self._uses_saved_layout() and self._original.get("kind") == self._mode()
                else None
            )
            self._roles = source_roles(self._mode(), saved_roles)
            saved_classification = (
                self._original.get("classification") if self._uses_saved_layout() else None
            )
            self._classification = (
                dict(saved_classification) if isinstance(saved_classification, dict) else {}
            )
            category = self.selected_category()
            self._category_links = (
                category_bindings(self._original)
                if schema is not None
                and self._uses_saved_layout()
                and category is not None
                and category.id == self._original.get("categoryId")
                else {}
            )
        if self._schema is not None:
            if not self._uses_saved_layout():
                self.editor.edited()
            if not self._original and not self.name.stringValue().strip():
                self.name.setStringValue_(self._schema.name[:120])
            reference = self.reference.stringValue().strip()
            if reference.startswith("https://") and not self.url.stringValue().strip():
                self.url.setStringValue_(reference)
        if same_table:
            self._choices = options
            self._draw(roles, preserve_scroll=True)
            if self._round_title is not None and previous_round is not None:
                self._round_title.selectItemAtIndex_(previous_round)
        else:
            self._draw_mapping(preserve_parent=first_saved_lookup and self._uses_saved_layout())
        if self._schema is not None and self._open_after_inspect:
            self._open_after_inspect = False
            self.open_column_setup()

    def _column_value(self, column: SourceColumn) -> str:
        """일반 표는 ID, 차시별 하위 표는 공통 이름으로 매핑한다."""
        return column.name if self._uses_rounds() else column.id

    def _uses_saved_layout(self) -> bool:
        """저장한 표와 차시 모드가 같을 때만 이전 속성 연결을 복원한다."""
        return uses_saved_layout(
            self._original,
            self._schema.id if self._schema else None,
            bool(self._profiles),
            self._uses_rounds(),
        )

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
        selected = {
            key: self._column_value(column)
            for key in self._mapping
            if (column := self._chosen(key)) is not None
        }
        original = self._original.get("properties") if self._uses_saved_layout() else None
        return selected_properties(selected, original, self._uses_rounds())

    def _default_choices(self) -> dict[str, int]:
        """이미 저장된 일정 분류와 휴가 승인 값을 같은 이름으로 복원한다."""
        kind = self._mode()
        if kind == "meeting":
            rules = self._original.get("eventTypes", {})
            if isinstance(rules, dict):
                return {
                    name: EVENT_CHOICES.index(value)
                    for name, value in rules.items()
                    if value in EVENT_CHOICES
                }
        if kind in ("task", "leave"):
            values = self._original.get(
                "approvedValues" if kind == "leave" else "completedStatuses"
            )
            if isinstance(values, list):
                return {str(name): 1 for name in values}
        return {}

    def _draw_mapping(self, *, preserve_parent: bool = False) -> None:
        """기존 매핑에 맞는 칼럼이나 조회 안내를 표시한다."""
        restore = self._uses_saved_layout() and self._mode() == self._original.get("kind")
        raw = self._original.get("properties") if restore else None
        selected: dict[str, str] = (
            {str(key): str(value) for key, value in raw.items()} if isinstance(raw, dict) else {}
        )
        if self._schema is not None and self._mode() != "leave" and "title" not in selected:
            if title := next(
                (column for column in self._schema.columns if column.kind == "title"), None
            ):
                selected["title"] = self._column_value(title)
        self._choices = self._default_choices() if restore else {}
        self._draw(selected, preserve_parent=preserve_parent)

    def _sync_parent_controls(self, preserve: bool) -> None:
        """부모 표의 숫자 칼럼을 표시하고 누락된 현재 연결도 보존한다."""
        nested = self._uses_rounds()
        self.parent_display.setHidden_(not nested)
        self.parent_sort.setHidden_(not nested)
        self._parent_hint.setHidden_(not nested)
        if not nested:
            return
        saved = self._original if self._uses_saved_layout() else {}
        number = saved.get("parentNumberProperty")
        legacy = "roundTitleProperty" in saved
        order = saved.get("parentOrder", "legacy" if legacy else "none")
        selected = (str(order), number if isinstance(number, str) else None)
        if preserve and self._parent_orders:
            selected = self._parent_orders[self.parent_sort.indexOfSelectedItem()]
        else:
            self.parent_display.setState_(int(saved.get("parentLabel", legacy) is True))
        numbers = (
            self._schema.parent_numbers
            if self._schema
            else ((number,) if isinstance(number, str) else ())
        )
        titles = ["부모 정렬 없음", "제목에서 ‘차’ 앞 숫자 읽기"]
        self._parent_orders = [("none", None), ("suffix", None)]
        if order == "legacy":
            titles.append("제목의 첫 숫자 (기존 설정)")
            self._parent_orders.append(("legacy", None))
        for name in numbers:
            titles.append(f"숫자 칼럼 · {name}")
            self._parent_orders.append(("column", name))
        # 숫자 칼럼이 개명되거나 타입이 바뀌어도 다른 설정을 저장할 때 연결을 잃지 않는다.
        if (
            selected[0] == "column"
            and selected[1] is not None
            and selected not in self._parent_orders
        ):
            titles.append(f"누락된 숫자 칼럼 · {selected[1]}")
            self._parent_orders.append(selected)
        set_titles(self.parent_sort, titles)
        self.parent_sort.selectItemAtIndex_(
            self._parent_orders.index(selected) if selected in self._parent_orders else 0
        )

    def _draw(
        self,
        selected: dict[str, str],
        *,
        preserve_scroll: bool = False,
        preserve_parent: bool = False,
    ) -> None:
        """칼럼 종류와 상태 행 수에 맞춰 카드 높이를 계산한다."""
        offset = 0.0
        if preserve_scroll:
            old_height = self.document.frame()[1][1]
            origin = self._page.contentView().bounds()[0][1]
            offset = max(0.0, old_height - self._viewport - origin)
        if self._body is not None:
            self._body.setHidden_(True)
            self.document.setSubviews_([self._sources_view, self._header, self._detail])
        self._role_names.clear()
        self._mapping.clear()
        self._options.clear()
        self._round_title = None
        self._sync_parent_controls(preserve_scroll or preserve_parent)
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
            for role in self._roles
            if role.key in selected or any(column.kind in role.types for column in schema.columns)
        )
        # 완료·일정·승인 판정은 자유롭게 정하는 표시 이름과 별도로 원본 값에 적용한다.
        rule_role = next(
            (role for role in roles if role.key in {"status", "approval", "category"}),
            None,
        )
        rule_column = next(
            (
                col
                for col in schema.columns
                if rule_role
                and col.kind in rule_role.types
                and self._same_value(col, selected.get(rule_role.key, ""))
            ),
            None,
        )
        self._rule_property = selected.get(rule_role.key) if rule_role else None
        options = (
            tuple(dict.fromkeys((*rule_column.options, *self._choices))) if rule_column else ()
        )
        height = 84 + 32 * (len(roles) + bool(schema.round_titles))
        if options:
            # 소제목과 다음 상위 행 사이의 여백은 하위 설정이 있을 때만 확보한다.
            height += 32 + 32 * len(options)
        card(body, 0, 0, 628, height)
        label(body, "칸 연결", 12, height - 32, 360, bold=True)
        y: float = height - 63
        if schema.round_titles:
            label(body, "부모 제목", 12, y + 4, 108)
            self._round_title = self._popup(
                body, 160, y, 446, list(schema.round_titles), "changed:"
            )
            saved = (
                self._original.get("parentTitleProperty", self._original.get("roundTitleProperty"))
                if self._uses_saved_layout()
                else None
            )
            self._round_title.selectItemAtIndex_(
                schema.round_titles.index(str(saved)) if saved in schema.round_titles else 0
            )
            y -= 32
        for role in roles:
            self._draw_role(body, role, schema, selected, y)
            y -= 32
            if role == rule_role and rule_column and options:
                y = self._draw_rule_options(body, y, rule_column, kind, options)
        self._role_types = tuple(
            dict.fromkeys(col.kind for col in schema.columns if col.kind in DISPLAY_COLUMN_TYPES)
        )
        self._new_role_type = self._popup(body, 160, 6, 225, list(self._role_types), "changed:")
        button(body, "역할 추가", 400, 6, 206, self._actions, "addRole:").setEnabled_(
            bool(self._role_types)
        )
        body.setFrameSize_((628, height))
        self._update_sheet()

    def _draw_rule_options(
        self,
        body: NSView,
        y: float,
        column: SourceColumn,
        kind: SourceKind,
        options: tuple[str, ...],
    ) -> float:
        """선택한 칼럼 바로 아래에 그 칼럼의 옵션별 설정을 들여쓴다."""
        operation = {
            "task": "완료로 처리할 값",
            "meeting": "알림·조회용 일정 유형",
            "leave": "승인으로 처리할 값",
        }[kind]
        heading = f"‘{column.name}’ 칼럼의 {operation}"
        label(body, heading, 36, y + 6, 570, bold=True, secondary=True).setToolTip_(heading)
        y -= 24
        for name in options:
            if kind == "meeting":
                label(body, name, 36, y + 4, 362).setToolTip_(name)
                control = self._popup(body, 410, y, 196, ["기타", "회의", "휴가"], "rules:")
                control.selectItemAtIndex_(self._choices.get(name, 0))
                self._options[name] = control
            else:
                check = NSButton.checkboxWithTitle_target_action_(name, self._actions, "changed:")
                check.setFrame_(frame(36, y, 570, 26))
                check.setToolTip_(name)
                check.setState_(self._choices.get(name, 0))
                body.addSubview_(check)
                self._options[name] = check
            y -= 32
        return y - 8

    def _summary(self, offset: float = 0.0) -> None:
        """메인 폼에는 편집기 대신 선택 상태와 열기 버튼을 둔다."""
        if self._body is not None:
            self.document.setSubviews_([self._sources_view, self._header, self._detail])
        self._refresh_sort()
        height = card_height(1)
        body = NSView.alloc().initWithFrame_(frame(16, 0, CARD_WIDTH, height))
        self._body = body
        card(body, 0, 0, CARD_WIDTH, height)
        title = label(body, "카테고리에 보일 칸", 0, 0, 300, size=13, bold=True)
        place_label(title, CARD_PADDING, height - CARD_PADDING - CARD_TITLE, 300, CARD_TITLE)
        selected = self.selected_category()
        config = selected.configuration if selected is not None else None
        message = (
            "카테고리를 선택한 뒤 표를 연결하세요."
            if selected is None
            else "보일 칸은 카테고리 설정에서 추가할 수 있습니다."
            if config is None or not config.columns
            else f"카테고리 칸 {len(config.columns)}개 중 {len(self._category_links)}개 연결됨"
        )
        hint = label(body, message, 0, 0, CARD_WIDTH - 2 * CARD_PADDING, secondary=True)
        place_label(
            hint, CARD_PADDING, form_row_y(height, 0), CARD_WIDTH - 2 * CARD_PADDING, FORM_ROW
        )
        self._setup_columns = button(
            body,
            "칸 연결…",
            CARD_WIDTH - CARD_PADDING - 96 + BUTTON_BEZEL_INSET,
            height - CARD_PADDING - CARD_TITLE - 3,
            96,
            self._actions,
            "setupColumns:",
        )
        self.document.addSubview_(body)
        # 새 연결은 표를 불러오기 전까지 보일 칸 카드를 숨긴다.
        self._layout(height if self._original or self._schema is not None else 0, offset)

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
        previous_definitions = self._roles
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
                self._roles = previous_definitions
                self._choices = previous_options
                self._draw(previous_roles, preserve_scroll=True)
                if self._round_title is not None and previous_round is not None:
                    self._round_title.selectItemAtIndex_(previous_round)
            self._summary()
            self.render()

        choices = tuple(
            SourceColumn(
                self._column_value(column),
                column.name,
                column.kind,
                column.options,
                column.option_ids,
            )
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
            classification=self._classification,
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
        if self._column_panel is not None:
            self._classification = self._column_panel.classification.draft()
        self._category_links = selected
        return None

    def _validate_roles(self, values: dict[str, str]) -> None:
        """켜 둔 동작에 필요한 칼럼만 요구한다."""
        self._read_role_names()
        validate_roles(self._roles, values)

    def _same_value(self, column: SourceColumn, value: str) -> bool:
        """이전 Notion 속성 ID의 인코딩 표기도 함께 인식한다."""
        candidate = self._column_value(column)
        return matches_property(candidate, value, self._uses_rounds())

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
        name = self._field(12, y, 144, "역할 이름", parent=body)
        name.setStringValue_(role.title)
        self._role_names[role.key] = name
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
        chosen = next(
            (
                index
                for index, column in enumerate(columns, 1)
                if self._same_value(column, selected.get(role.key, ""))
            ),
            0,
        )
        # 새 연결은 종류가 맞는 첫 칸을 미리 골라 두어 사용자는 확인만 하면 된다.
        if (
            not chosen
            and columns
            and not self._original
            and role.key in AUTO_ROLES
            and role.key not in selected
        ):
            chosen = 1
        popup.selectItemAtIndex_(chosen)
        self._mapping[role.key] = popup, columns

    def _read_role_names(self) -> None:
        """다시 그리기 전에 편집한 이름을 역할 ID에 보존한다."""
        self._roles = tuple(
            replace(role, title=self._role_names[role.key].stringValue().strip())
            if role.key in self._role_names
            else role
            for role in self._roles
        )

    def add_role(self) -> None:
        """새 역할의 ID는 표시 이름이나 원본 연결을 변경해도 유지한다."""
        self._read_role_names()
        kind = self._role_types[self._new_role_type.indexOfSelectedItem()]
        self._roles += (ColumnRole(str(uuid4()), "새 역할", (kind,), False),)
        self.change_columns()

    def change_columns(self) -> None:
        """바뀐 역할에 따라 연결된 상태 옵션만 다시 계산한다."""
        self.mark_edited()
        self._read_role_names()
        round_index = self._round_title.indexOfSelectedItem() if self._round_title else None
        selected = self._selected_columns()
        key = {"task": "status", "meeting": "category", "leave": "approval"}[self._mode()]
        self._choices = self._selected_options() if selected.get(key) == self._rule_property else {}
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
        nested = self._uses_rounds()
        # 하위 표를 모을 때만 부모 항목 줄과 안내 줄이 끼어든다.
        rows = ["url", "sort", "rounds", *(("parent", "hint") if nested else ()), "remove"]
        detail_height = card_height(len(rows)) if self._options_open else COLLAPSED_OPTIONS
        sources_height = self._sources_view.frame()[1][1]
        header_height = self._place_header()
        sources_gap = CARD_GAP if sources_height else 0.0
        top_height = sources_height + sources_gap + header_height
        height = max(
            self._viewport,
            PAGE_TOP
            + top_height
            + CARD_GAP
            + (body_height + CARD_GAP if body_height else 0)
            + detail_height
            + CARD_PADDING,
        )
        self.document.setFrameSize_((584, height))
        top = height - PAGE_TOP
        self._sources_view.setFrame_(frame(16, top - sources_height, CARD_WIDTH, sources_height))
        top -= sources_height + sources_gap
        self._header.setFrame_(frame(16, top - header_height, CARD_WIDTH, header_height))
        top -= header_height + CARD_GAP
        if self._body is not None:
            self._body.setHidden_(body_height == 0)
            self._body.setFrame_(frame(16, top - body_height, CARD_WIDTH, body_height))
        if body_height:
            top -= body_height + CARD_GAP
        self._detail.setFrame_(frame(16, top - detail_height, CARD_WIDTH, detail_height))
        self._detail_card.setFrame_(frame(0, 0, CARD_WIDTH, detail_height))
        title_y = detail_height - CARD_PADDING - CARD_TITLE
        place_label(self._detail_title, CARD_PADDING, title_y, 300, CARD_TITLE)
        self._expand_options.setFrame_(
            frame(CARD_WIDTH - CARD_PADDING - 80 + BUTTON_BEZEL_INSET, title_y - 3, 80, 26)
        )
        self._expand_options.setTitle_("접기" if self._options_open else "펼치기")
        # 펼친 영역이 제목 줄을 덮으면 ‘접기’ 버튼이 클릭을 받지 못하므로 제목 줄 아래까지만 둔다.
        self._advanced.setFrame_(
            frame(0, 0, CARD_WIDTH, max(0.0, detail_height - CARD_PADDING - CARD_TITLE))
        )
        self._advanced.setHidden_(not self._options_open)
        if self._options_open:
            self._place_advanced(detail_height, rows)
        self.document.scrollPoint_((0, max(0, height - self._viewport - offset)))

    def _place_advanced(self, height: float, rows: list[str]) -> None:
        """고급 카드의 줄을 간격 체계에 맞춰 놓고 줄 사이 구분선을 옮긴다."""
        y = {key: form_row_y(height, index) for index, key in enumerate(rows)}
        for key, name in self._detail_labels.items():
            name.setHidden_(key not in y)
            if key in y:
                place_label(name, CARD_PADDING, y[key], FORM_LABEL_WIDTH - 8, FORM_ROW)
        self.url.setFrame_(frame(FORM_CONTROL_X, form_control_y(y["url"], 24), 300, 24))
        self.sort.setFrame_(frame(FORM_CONTROL_X - 3, form_control_y(y["sort"], 26), 220, 26))
        self.sort_direction.setFrame_(
            frame(FORM_CONTROL_X + 224, form_control_y(y["sort"], 26), 150, 26)
        )
        self.rounds.setFrame_(frame(FORM_CONTROL_X, form_control_y(y["rounds"], 24), 300, 24))
        if "parent" in y:
            self.parent_display.setFrame_(
                frame(FORM_CONTROL_X, form_control_y(y["parent"], 24), 140, 24)
            )
            self.parent_sort.setFrame_(
                frame(FORM_CONTROL_X + 144, form_control_y(y["parent"], 26), 230, 26)
            )
            place_label(
                self._parent_hint,
                FORM_CONTROL_X,
                y["hint"],
                CARD_WIDTH - FORM_CONTROL_X - CARD_PADDING,
                FORM_ROW,
            )
        self._remove.setFrame_(
            frame(FORM_CONTROL_X - BUTTON_BEZEL_INSET, form_control_y(y["remove"], 26), 100, 26)
        )
        while len(self._detail_dividers) < len(rows) - 1:
            self._detail_dividers.append(form_divider(self._advanced, 0))
        for index, divider in enumerate(self._detail_dividers):
            divider.setHidden_(index >= len(rows) - 1)
            if index < len(rows) - 1:
                divider.setFrame_(
                    frame(
                        CARD_PADDING,
                        form_row_y(height, index) - FORM_ROW_GAP / 2,
                        CARD_WIDTH - 2 * CARD_PADDING,
                        1,
                    )
                )

    def draft(self) -> SourceDraft:
        """현재 편집값 또는 삭제 선택을 조회한 버전의 초안으로 전달한다."""
        state = self.editor.state
        if state is None:
            raise SourceSettingsError("수집 설정을 다시 불러오세요.")
        return source_draft(
            state.settings, self._selected, None if self._remove_pending else self._read_edit()
        )

    def _read_edit(self) -> SourceEdit:
        """선택한 컨트롤을 화면과 독립적인 값으로 읽는다."""
        category = self.selected_category()
        table = None
        if self._schema is not None:
            values = self._selected_columns()
            self._read_role_names()
            table = TableEdit(
                self._schema.id,
                values,
                {key: col.name[:250] for key in values if (col := self._chosen(key)) is not None},
                self._roles,
                self._schema.round_titles[self._round_title.indexOfSelectedItem()]
                if self._round_title is not None
                else None,
                self._selected_options(),
            )
        return SourceEdit(
            category_id=category.id if category else None,
            reference=self.reference.stringValue().strip(),
            lookup_reference=self._lookup_reference,
            has_profiles=bool(self._profiles),
            kind=self._mode(),
            section=self._section(),
            table=table,
            parent_order=self._parent_orders[self.parent_sort.indexOfSelectedItem()]
            if self._uses_rounds()
            else None,
            parent_label=bool(self.parent_display.state()),
            classification=self._classification,
            links=self._category_links,
            name=self.name.stringValue().strip(),
            url=self.url.stringValue(),
            source_id=self._source_id,
            sort=self._sort_keys[self.sort.indexOfSelectedItem()],
            descending=self.sort_direction.indexOfSelectedItem() == 1,
        )

    def render(self) -> None:
        """확정 조회·저장 때만 폼을 재구성하고 나머지 입력을 보존한다."""
        self.poll()
        state = self.editor.state
        if state is not None and self._generation != self.editor.generation:
            self._generation = self.editor.generation
            sources = state.settings.sources
            self._selected = min(self._selected, len(sources))
            names = [source_menu_name(raw, i + 1) for i, raw in enumerate(sources)]
            # 같은 표 이름과 별칭을 사용하는 뷰도 선택 목록에서 구별한다.
            counts = Counter(names)
            set_titles(
                self.list,
                [f"{name} ({i})" if counts[name] > 1 else name for i, name in enumerate(names, 1)]
                + ["새 소스"],
            )
            self.list.selectItemAtIndex_(self._selected)
            self._fill(sources[self._selected] if self._selected < len(sources) else {})
            self._draw_sources()
            self._layout(self._body_height)
        editable = self.editor.editable and not self.busy
        self.list.setEnabled_(editable and not self.editor.dirty)
        for row in self._source_rows:
            row.setEnabled_(editable and not self.editor.dirty)
        self._header_title.setStringValue_(
            self.name.stringValue().strip() or ("새 연결" if not self._original else "연결")
        )
        self._find.setEnabled_(
            editable and not self._remove_pending and self._tables_pending is None
        )
        self._remove.setTitle_("해제 취소" if self._remove_pending else "연결 해제")
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
            self.parent_display,
            self.parent_sort,
            self.sort,
            *self._options.values(),
        ):
            control.setEnabled_(enabled)
        self.sort_direction.setEnabled_(enabled and self.sort.indexOfSelectedItem() > 0)
        self._new.setEnabled_(editable and not self.editor.dirty)
        self._manage.setEnabled_(editable and not self.editor.dirty)
        if not self._categories:
            for control in (
                self.name,
                self.reference,
                self._inspect,
                self._find,
                self.category,
                self.mode,
            ):
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
