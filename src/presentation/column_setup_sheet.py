"""수집 소스의 원본 속성을 이미 등록된 카테고리 칼럼에 연결한다."""

from collections.abc import Callable, Mapping
from urllib.parse import unquote
from weakref import proxy

from AppKit import (
    NSBackingStoreBuffered,
    NSMenuItem,
    NSNoBorder,
    NSPanel,
    NSPopUpButton,
    NSScrollView,
    NSSegmentedControl,
    NSSegmentSwitchTrackingSelectOne,
    NSView,
    NSWindow,
    NSWindowStyleMaskTitled,
)
from Foundation import NSObject

from src.application.category_column_types import accepts_column_type
from src.application.source_settings import SourceColumn, SourceSettingsError
from src.domain.category_configuration import CategoryColumn, ColumnBinding
from src.presentation.native_views import button, frame, label
from src.presentation.source_classification_editor import SourceClassificationEditor

TYPE_LABELS = {
    "text": "텍스트",
    "number": "숫자",
    "date": "날짜",
    "time": "시간",
    "boolean": "참/거짓",
}


class ColumnSheetActions(NSObject):
    """수집 연결 시트의 탭과 완료 조작이다."""

    owner: "ColumnSetupSheet"

    def tab_(self, sender: NSSegmentedControl) -> None:
        """현재 작업을 유지하고 다른 연결 화면을 보여 준다."""
        self.owner.select_tab(sender.selectedSegment())

    def apply_(self, sender: object) -> None:
        """현재 소스가 연결한 원본 속성을 저장 초안으로 전달한다."""
        self.owner.apply()

    def cancel_(self, sender: object) -> None:
        """연결을 바꾸지 않고 설정으로 돌아간다."""
        self.owner.finish(False)


class ColumnSetupSheet:
    """카테고리 정의를 그대로 읽고 원본 속성만 선택하게 한다."""

    def __init__(
        self,
        parent: NSWindow,
        source_name: str,
        category_name: str,
        columns: tuple[CategoryColumn, ...],
        available: tuple[SourceColumn, ...],
        links: Mapping[str, ColumnBinding],
        apply: Callable[[dict[str, ColumnBinding]], str | None],
        closed: Callable[[bool], None],
        *,
        rounds: bool = False,
        classification: dict[str, object] | None = None,
    ) -> None:
        """현재 카테고리와 소스 조회기의 결과로 두 페이지를 구성한다."""
        self.parent, self._apply, self._closed = parent, apply, closed
        self._links, self._rounds = links, rounds
        self._connections: dict[str, tuple[NSPopUpButton, tuple[SourceColumn, ...]]] = {}
        self._actions = ColumnSheetActions.alloc().init()
        self._actions.owner = proxy(self)
        self.window = NSPanel.alloc().initWithContentRect_styleMask_backing_defer_(
            frame(0, 0, 686, 524), NSWindowStyleMaskTitled, NSBackingStoreBuffered, False
        )
        self.window.setTitle_("원본 칼럼 연결")
        self.window.setReleasedWhenClosed_(False)
        content = NSView.alloc().initWithFrame_(frame(0, 0, 686, 524))
        self.window.setContentView_(content)
        label(content, source_name, 22, 485, 642, size=17, bold=True)
        label(content, f"카테고리 · {category_name}", 22, 457, 642, secondary=True)
        titles = ("카테고리 칼럼", "수집에 사용할 속성", "분류 기준")
        self._tabs = NSSegmentedControl.alloc().initWithFrame_(frame(22, 411, 454, 32))
        self._tabs.setSegmentCount_(len(titles))
        self._tabs.setTrackingMode_(NSSegmentSwitchTrackingSelectOne)
        self._tabs.setTarget_(self._actions)
        self._tabs.setAction_("tab:")
        content.addSubview_(self._tabs)
        self._pages = []
        for index, title in enumerate(titles):
            self._tabs.setLabel_forSegment_(title, index)
            self._tabs.setWidth_forSegment_(148, index)
            page = NSView.alloc().initWithFrame_(frame(22, 54, 642, 342))
            content.addSubview_(page)
            self._pages.append(page)
        self._layout_columns(self._pages[0], columns, available, links)
        label(
            self._pages[1],
            "항목 제목과 날짜·필터에 사용할 원본 속성을 고르세요.",
            9,
            310,
            605,
            secondary=True,
        )
        self.classification = SourceClassificationEditor(
            self._pages[2],
            scroll(self._pages[2], 0, 39, 642, 210),
            available,
            classification or {},
            self.same_property,
        )
        self._roles = scroll(self._pages[1], 0, 0, 642, 290)
        self._status = label(content, "", 22, 18, 413, size=12, secondary=True)
        button(content, "취소", 465, 15, 90, self._actions, "cancel:").setKeyEquivalent_("\x1b")
        button(content, "적용", 568, 15, 96, self._actions, "apply:").setKeyEquivalent_("\r")
        self.select_tab(0)

    def _layout_columns(
        self,
        parent: NSView,
        columns: tuple[CategoryColumn, ...],
        available: tuple[SourceColumn, ...],
        links: Mapping[str, ColumnBinding],
    ) -> None:
        """등록된 칼럼 값으로 읽을 수 있는 Notion 속성만 보여 준다."""
        if not columns:
            label(
                parent,
                "이 카테고리에 저장 칼럼이 없습니다. 카테고리 설정에서 먼저 추가하세요.",
                8,
                302,
                622,
                secondary=True,
            )
            return
        label(parent, "카테고리 저장 칼럼", 10, 312, 210, bold=True)
        label(parent, "Notion 원본 속성", 255, 312, 374, bold=True)
        listing = scroll(parent, 0, 0, 642, 297)
        height = max(297, 16 + 38 * len(columns))
        rows = NSView.alloc().initWithFrame_(frame(0, 0, 628, height))
        for index, column in enumerate(columns):
            y = height - 36 - index * 38
            name = label(rows, column.name, 10, y + 5, 151)
            name.setToolTip_(column.name)
            label(rows, TYPE_LABELS[column.kind], 164, y + 5, 80, secondary=True)
            options = tuple(
                item for item in available if accepts_column_type("notion", column.kind, item.kind)
            )
            picker = NSPopUpButton.alloc().initWithFrame_pullsDown_(frame(255, y, 365, 26), False)
            picker.removeAllItems()
            for title in ("연결 안 함", *(item.name for item in options)):
                picker.menu().addItem_(
                    NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(title, None, "")
                )
            previous = links.get(column.id)
            selected = next(
                (
                    i
                    for i, item in enumerate(options, 1)
                    if previous is not None and self.same_property(item.id, previous.property_id)
                ),
                0,
            )
            picker.selectItemAtIndex_(selected)
            picker.setEnabled_(bool(options))
            if not options:
                picker.setToolTip_("이 표에는 값 종류가 맞는 속성이 없습니다.")
            rows.addSubview_(picker)
            self._connections[column.id] = (picker, options)
        listing.setDocumentView_(rows)
        rows.scrollPoint_((0, max(0, height - 297)))

    def show(self) -> None:
        """설정 창에 연결 시트를 연다."""
        self.parent.beginSheet_completionHandler_(self.window, self._complete)

    def select_tab(self, index: int) -> None:
        """연결 정보별 탭을 바꾼다."""
        self._tabs.setSelectedSegment_(index)
        for position, page in enumerate(self._pages):
            page.setHidden_(position != index)

    def update_behavior(self, view: NSView, height: float) -> None:
        """이전부터 쓰던 원본 속성 목록을 한 페이지에 보인다."""
        canvas = max(290, height)
        container = NSView.alloc().initWithFrame_(frame(0, 0, 628, canvas))
        container.addSubview_(view)
        view.setFrame_(frame(0, canvas - height, 628, height))
        self._roles.setDocumentView_(container)
        container.scrollPoint_((0, canvas - 290))

    def apply(self) -> None:
        """카테고리마다 같은 칸에 현재 소스의 원본 속성을 연결한다."""
        links = {}
        for key, (control, candidates) in self._connections.items():
            index = control.indexOfSelectedItem() - 1
            if 0 <= index < len(candidates):
                selected = candidates[index]
                previous = self._links.get(key)
                property_id = (
                    previous.property_id
                    if previous is not None
                    and self.same_property(selected.id, previous.property_id)
                    else selected.id
                )
                links[key] = ColumnBinding(property_id, selected.kind)
        try:
            self.classification.draft()
        except SourceSettingsError as error:
            self.select_tab(2)
            self._status.setStringValue_(str(error))
            return
        message = self._apply(links)
        if message:
            self._status.setStringValue_(message)
            self.select_tab(1)
            return
        self.finish(True)

    def same_property(self, selected: str, previous: str | None) -> bool:
        """일반 표는 ID 인코딩 차이를 허용하고 차시 이름은 정확히 비교한다."""
        if previous is None:
            return False
        return selected == previous if self._rounds else unquote(selected) == unquote(previous)

    def finish(self, applied: bool) -> None:
        """시트를 닫고 변경 여부를 전달한다."""
        self.parent.endSheet_returnCode_(self.window, int(applied))

    def _complete(self, result: int) -> None:
        """시트가 사라지면 부모 화면을 갱신한다."""
        self.window.orderOut_(None)
        self._closed(result == 1)


def scroll(parent: NSView, x: float, y: float, width: float, height: float) -> NSScrollView:
    """카테고리와 원본 연결에 같은 투명 스크롤 스타일을 적용한다."""
    view = NSScrollView.alloc().initWithFrame_(frame(x, y, width, height))
    view.setBorderType_(NSNoBorder)
    view.setDrawsBackground_(False)
    view.setHasVerticalScroller_(True)
    view.setAutohidesScrollers_(True)
    parent.addSubview_(view)
    return view
