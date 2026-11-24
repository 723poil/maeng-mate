"""Notion 계정과 조회 옵션을 편집하는 기본 macOS 폼이다."""

from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor

from AppKit import NSMenuItem, NSPopUpButton, NSScrollView, NSTextField, NSView
from Foundation import NSObject

from src.application.notion_people import NotionPeople
from src.application.runtime_settings import parse_runtime_settings
from src.domain.runtime_settings import RuntimeSettings
from src.presentation.native_views import (
    CARD_PADDING,
    CARD_TITLE,
    CARD_WIDTH,
    FORM_CONTROL_X,
    FORM_LABEL_WIDTH,
    FORM_ROW,
    card,
    card_height,
    form_control_y,
    form_dividers,
    form_row_y,
    frame,
    label,
    place_label,
)
from src.presentation.runtime_settings_controller import RuntimeSettingsController

# 페이지 맨 위 카드와 스크롤 영역 위쪽 사이 여백이다.
PAGE_TOP = 8.0
# 계정 카드는 ID·목록 범위 두 줄이다.
ACCOUNT_HEIGHT = card_height(2)
# 기본 입력칸 높이와 목록 범위 선택 폭이다.
FIELD_HEIGHT = 22.0
POLICY_WIDTH = 240.0


def card_title(parent: NSView, text: str, height: float) -> None:
    """카드 제목은 왼쪽 위 안쪽 여백에 같은 크기로 둔다."""
    title = label(parent, text, CARD_PADDING, 0, 300, size=13, bold=True)
    place_label(title, CARD_PADDING, height - CARD_PADDING - CARD_TITLE, 300, CARD_TITLE)


def row_label(parent: NSView, text: str, row_y: float) -> NSTextField:
    """항목 이름은 왼쪽 칸에서 입력 줄 높이의 가운데에 둔다."""
    name = label(parent, text, CARD_PADDING, 0, FORM_LABEL_WIDTH - 8)
    place_label(name, CARD_PADDING, row_y, FORM_LABEL_WIDTH - 8, FORM_ROW)
    return name


def row_caption(parent: NSView, text: str, row_y: float) -> NSTextField:
    """카드 아래쪽 보조 설명은 한 줄 전체 폭에 흐린 글자로 둔다."""
    caption = label(parent, text, CARD_PADDING, 0, CARD_WIDTH - 2 * CARD_PADDING, secondary=True)
    place_label(caption, CARD_PADDING, row_y, CARD_WIDTH - 2 * CARD_PADDING, FORM_ROW)
    return caption


def form_field(parent: NSView, row_y: float, width: float, hint: str) -> NSTextField:
    """입력칸은 공통 입력 시작선에서 기본 높이로 줄 가운데에 둔다."""
    field = NSTextField.textFieldWithString_("")
    field.setFrame_(frame(FORM_CONTROL_X, form_control_y(row_y, FIELD_HEIGHT), width, FIELD_HEIGHT))
    field.setPlaceholderString_(hint)
    parent.addSubview_(field)
    return field


class RuntimeSettingsForm:
    """페이지 전환·재열기에서 입력 초안을 유지하는 계정 카드다."""

    def __init__(
        self,
        notion_page: NSScrollView,
        viewport: float,
        controller: RuntimeSettingsController,
        actions: NSObject,
        people: Callable[[], NotionPeople] | None = None,
    ) -> None:
        """Notion 페이지 문서를 만들고 맨 위에 계정 카드를 놓는다."""
        self.controller = controller
        self._generation = 0
        # 사람 목록은 토큰이 있을 때 한 번 읽고, 못 읽으면 ID 직접 입력으로 대신한다.
        self._people_source = people
        self._people: NotionPeople | None = None
        self._people_future: Future[NotionPeople] | None = None
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="notion-people")
        self._person_ids: list[str | None] = []
        self._person_titles: list[str] = []
        self._manual = False
        # 아래 토큰·링크 카드가 필요하면 문서를 늘리므로 처음에는 보이는 높이로 만든다.
        notion = NSView.alloc().initWithFrame_(frame(0, 0, 584, viewport))
        notion_page.setDocumentView_(notion)
        height = ACCOUNT_HEIGHT
        section = NSView.alloc().initWithFrame_(
            frame(CARD_PADDING, viewport - PAGE_TOP - height, CARD_WIDTH, height)
        )
        notion.addSubview_(section)
        card(section, 0, 0, CARD_WIDTH, height)
        # 설명 줄은 입력 줄이 아니므로 위 두 줄 사이에만 구분선을 긋는다.
        form_dividers(section, height, 2)
        card_title(section, "내 계정", height)

        row = form_row_y(height, 0)
        row_label(section, "사용자", row)
        # 저장값은 ID 입력칸이 가진다. 사람 목록을 읽으면 이름 메뉴로 보여 주고 고른 ID를 칸에 쓴다.
        self.user_id = form_field(
            section, row, CARD_WIDTH - CARD_PADDING - FORM_CONTROL_X, "Notion 사용자 ID"
        )
        self.user_id.setDelegate_(actions)
        self.user_id.setToolTip_("담당자·참여자 중 나를 찾을 때 이 ID를 씁니다.")
        self.person = NSPopUpButton.alloc().initWithFrame_pullsDown_(
            frame(FORM_CONTROL_X - 3, form_control_y(row, 26), POLICY_WIDTH, 26), False
        )
        self.person.setToolTip_("담당자·참여자 중 나를 찾을 때 씁니다.")
        self.person.setTarget_(actions)
        self.person.setAction_("personChosen:")
        self.person.setHidden_(True)
        section.addSubview_(self.person)

        row = form_row_y(height, 1)
        row_label(section, "목록 범위", row)
        self.policy = NSPopUpButton.alloc().initWithFrame_pullsDown_(
            frame(FORM_CONTROL_X - 3, form_control_y(row, 26), POLICY_WIDTH, 26), False
        )
        self.policy.addItemsWithTitles_(["담당 업무", "담당 업무와 참여 일정"])
        self.policy.setToolTip_(
            "사람 속성이 있는 표에서 내가 참여한 일정도 목록에 보일지 정합니다."
        )
        self.policy.setTarget_(actions)
        self.policy.setAction_("runtimeEdited:")
        section.addSubview_(self.policy)

        self.render()

    def draft(self) -> RuntimeSettings:
        """입력 범위와 버전을 검증한 뒤 저장 후보를 만든다."""
        state = self.controller.state
        if state is None:
            raise ValueError("먼저 설정을 불러오세요.")
        return parse_runtime_settings(
            {
                "revision": state.settings.revision,
                "general": {
                    "relevancePolicy": "assignment"
                    if self.policy.indexOfSelectedItem() == 0
                    else "participation",
                    # 예약이 없는 자료의 내부 기본값은 화면 편집 없이 보존한다.
                    "snapshotMaxAgeSeconds": state.settings.general.snapshot_max_age_seconds,
                },
                "providers": {
                    "notion": {"userId": self.user_id.stringValue().strip() or None},
                    "slack": {"userId": state.settings.slack.user_id},
                },
            }
        )

    def load_people(self) -> None:
        """토큰이 등록된 뒤 사람 목록을 한 번만 백그라운드에서 읽는다."""
        if self._people_source is None or self._people is not None or self._people_future:
            return
        self._people_future = self._executor.submit(self._people_source)

    def forget_people(self) -> None:
        """토큰이 바뀌면 다른 워크스페이스일 수 있으므로 목록을 다시 읽게 한다."""
        self._people = None
        self._manual = False

    def choose_person(self) -> None:
        """이름을 고르면 그 ID를 저장 초안에 쓰고, 마지막 항목이면 ID 입력칸을 연다."""
        index = self.person.indexOfSelectedItem()
        if 0 <= index < len(self._person_ids):
            self.user_id.setStringValue_(self._person_ids[index] or "")
            self.controller.edited()
            return
        self._manual = True
        self._sync_person()
        window = self.user_id.window()
        if window is not None:
            window.makeFirstResponder_(self.user_id)

    def _collect_people(self) -> None:
        """끝난 조회만 반영한다. 실패하면 목록 없이 ID 입력칸을 그대로 쓴다."""
        future = self._people_future
        if future is None or not future.done():
            return
        self._people_future = None
        try:
            self._people = future.result()
        except Exception:
            self._people = NotionPeople()

    def _sync_person(self) -> None:
        """현재 ID를 이름으로 보여 주고, 목록에 없는 ID는 맨 위 항목으로 남긴다."""
        people = self._people.people if self._people is not None else ()
        picker = bool(people) and not self._manual
        self.person.setHidden_(not picker)
        self.user_id.setHidden_(picker)
        if not picker:
            return
        current = self.user_id.stringValue().strip()
        ids: list[str | None] = [person.id for person in people]
        titles = [person.name for person in people]
        if current and current not in ids:
            ids.insert(0, current)
            titles.insert(0, f"ID {current[:8]}…")
        elif not current:
            ids.insert(0, None)
            titles.insert(0, "선택 안 함")
        if ids != self._person_ids or titles != self._person_titles:
            # 같은 이름이 둘이어도 항목 수가 줄지 않도록 메뉴 항목을 직접 만든다.
            self.person.removeAllItems()
            for title in titles:
                item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(title, None, "")
                self.person.menu().addItem_(item)
            self.person.menu().addItem_(NSMenuItem.separatorItem())
            manual = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_("ID로 입력…", None, "")
            self.person.menu().addItem_(manual)
            self._person_ids, self._person_titles = ids, titles
        self.person.selectItemAtIndex_(ids.index(current or None))

    def render(self) -> None:
        """새 조회·저장 성공에서만 값을 채워 입력 중인 초안을 덮지 않는다."""
        controller = self.controller
        if controller.state is not None and self._generation != controller.generation:
            value = controller.state.settings
            self.user_id.setStringValue_(value.notion.user_id or "")
            self.policy.selectItemAtIndex_(
                0 if value.general.relevance_policy == "assignment" else 1
            )
            self._generation = controller.generation
        self._collect_people()
        self._sync_person()
        for control in (self.user_id, self.policy, self.person):
            control.setEnabled_(controller.editable)
