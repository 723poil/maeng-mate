"""표시할 영역을 실제 순서대로 나열하고 네이티브 드래그로 이동한다."""

from collections.abc import Callable
from dataclasses import replace
from typing import Protocol

from AppKit import (
    NSButton,
    NSColor,
    NSDragOperationMove,
    NSDragOperationNone,
    NSFont,
    NSNoBorder,
    NSPasteboard,
    NSPasteboardItem,
    NSScrollView,
    NSSwitchButton,
    NSTableColumn,
    NSTableView,
    NSTableViewDropAbove,
    NSTableViewSelectionHighlightStyleNone,
    NSTableViewStylePlain,
    NSView,
)
from Foundation import NSObject

from src.domain.preferences import SECTIONS, SectionPreference
from src.presentation.native_views import button, frame, symbol

SECTION_LABELS = {
    "work": "근무",
    "calendar": "캘린더",
}
DRAG_TYPE = "local.maeng-mate.section-order"


class DraggingInfo(Protocol):
    """순서 이동에 필요한 출처와 내부 붙여넣기판만 읽는다."""

    def draggingSource(self) -> object:
        """자기 목록 안의 이동인지 확인한다."""
        ...

    def draggingPasteboard(self) -> NSPasteboard:
        """영역 키만 담은 붙여넣기판을 읽는다."""
        ...


class SectionRowView(NSView):
    """행을 잡는 동작을 설정 창 전체의 이동으로 바꾸지 않는다."""

    def mouseDownCanMoveWindow(self) -> bool:
        """투명한 행의 빈 공간도 목록 드래그가 처리한다."""
        return False


class SectionOrderActions(NSObject):
    """Cocoa 목록의 행 표시와 드래그 콜백을 연결한다."""

    owner: "SectionOrderList"

    def numberOfRowsInTableView_(self, table: NSTableView) -> int:
        """확정 또는 최신 초안 순서의 영역 수를 반환한다."""
        return len(self.owner.sections)

    def tableView_viewForTableColumn_row_(
        self, table: NSTableView, column: NSTableColumn | None, row: int
    ) -> NSView | None:
        """표시 체크박스와 이동 손잡이를 한 행에 놓는다."""
        return self.owner.make_row(row)

    def changed_(self, sender: NSButton) -> None:
        """행의 표시 선택만 바꾸고 위치는 유지한다."""
        self.owner.toggle(sender.tag(), sender.state() == 1)

    def tableView_pasteboardWriterForRow_(
        self, table: NSTableView, row: int
    ) -> NSPasteboardItem | None:
        """드래그 데이터에는 공개 영역 키 외의 업무 자료를 넣지 않는다."""
        if not self.owner.enabled or not 0 <= row < len(self.owner.sections):
            return None
        item = NSPasteboardItem.alloc().init()
        return item if item.setString_forType_(self.owner.sections[row].key, DRAG_TYPE) else None

    def tableView_validateDrop_proposedRow_proposedDropOperation_(
        self, table: NSTableView, info: DraggingInfo, row: int, operation: int
    ) -> int:
        """외부 끌어놓기는 받지 않고 행 사이 이동만 허용한다."""
        if (
            not self.owner.enabled
            or info.draggingSource() != table
            or not 0 <= row <= len(self.owner.sections)
        ):
            return NSDragOperationNone
        table.setDropRow_dropOperation_(row, NSTableViewDropAbove)
        return NSDragOperationMove

    def tableView_acceptDrop_row_dropOperation_(
        self, table: NSTableView, info: DraggingInfo, row: int, operation: int
    ) -> bool:
        """현재 목록에서 잡은 영역을 삽입 위치로 이동한다."""
        key = info.draggingPasteboard().stringForType_(DRAG_TYPE)
        if info.draggingSource() != table or key is None:
            return False
        return self.owner.move(key, row)


class SectionOrderList:
    """영역 순서를 보이는 목록과 저장 초안에서 함께 유지한다."""

    def __init__(
        self,
        parent: NSView,
        x: float,
        y: float,
        width: float,
        changed: Callable[[tuple[SectionPreference, ...]], None],
    ) -> None:
        """같은 행 뷰를 이동시켜 별도 순위 셀렉터를 만들지 않는다."""
        self.sections: tuple[SectionPreference, ...] = ()
        self.enabled = False
        self.checks: dict[str, NSButton] = {}
        self._changed = changed
        self._width = width
        self._tags: tuple[str, ...] = ()
        self._labels = dict(SECTION_LABELS)
        self._actions = SectionOrderActions.alloc().init()
        self._actions.owner = self
        height = len(SECTIONS) * 32
        self._scroll = NSScrollView.alloc().initWithFrame_(frame(x, y, width, height))
        self._scroll.setHasVerticalScroller_(True)
        self._scroll.setAutohidesScrollers_(True)
        self._scroll.setDrawsBackground_(False)
        self._scroll.setBorderType_(NSNoBorder)
        self.view = NSTableView.alloc().initWithFrame_(frame(0, 0, width, height))
        column = NSTableColumn.alloc().initWithIdentifier_("section")
        column.setWidth_(width)
        self.view.addTableColumn_(column)
        self.view.setHeaderView_(None)
        self.view.setStyle_(NSTableViewStylePlain)
        self.view.setBackgroundColor_(NSColor.clearColor())
        self.view.setRowHeight_(32)
        self.view.setIntercellSpacing_((0, 0))
        self.view.setSelectionHighlightStyle_(NSTableViewSelectionHighlightStyleNone)
        self.view.setAllowsMultipleSelection_(False)
        self.view.setAllowsEmptySelection_(True)
        self.view.setVerticalMotionCanBeginDrag_(True)
        self.view.registerForDraggedTypes_([DRAG_TYPE])
        self.view.setDraggingSourceOperationMask_forLocal_(NSDragOperationMove, True)
        self.view.setDraggingSourceOperationMask_forLocal_(NSDragOperationNone, False)
        self.view.setDataSource_(self._actions)
        self.view.setDelegate_(self._actions)
        self._scroll.setDocumentView_(self.view)
        parent.addSubview_(self._scroll)

    def make_row(self, index: int) -> NSView | None:
        """선택된 순서의 이름과 표시 여부를 기본 macOS 컨트롤로 그린다."""
        if not 0 <= index < len(self.sections):
            return None
        item = self.sections[index]
        row = SectionRowView.alloc().initWithFrame_(frame(0, 0, self._width, 32))
        check = button(
            row,
            self._labels.get(item.key, item.key),
            0,
            3,
            self._width - 46,
            self._actions,
            "changed:",
            bordered=False,
        )
        check.setButtonType_(NSSwitchButton)
        check.setFont_(NSFont.systemFontOfSize_(13))
        check.setTag_(self._tags.index(item.key))
        check.setState_(int(item.visible))
        check.setEnabled_(self.enabled)
        self.checks[item.key] = check
        handle = symbol(row, "line.3.horizontal", self._width - 28, 6.5)
        handle.setToolTip_("끌어서 순서 변경")
        return row

    def resize(self, top: float, height: float) -> None:
        """실제 영역 개수만큼 표시하고 공간이 좁을 때 내부 스크롤을 사용한다."""
        x = self._scroll.frame()[0][0]
        self._scroll.setFrame_(frame(x, top - height, self._width, height))

    def update(
        self,
        sections: tuple[SectionPreference, ...],
        enabled: bool,
        names: dict[str, str] | None = None,
    ) -> None:
        """저장 상태만 변하면 열린 목록과 드래그 상태를 다시 만들지 않는다."""
        labels = {**SECTION_LABELS, **(names or {})}
        changed = sections != self.sections or labels != self._labels
        self.sections, self.enabled, self._labels = sections, enabled, labels
        self.view.setEnabled_(enabled)
        if changed:
            self._tags = tuple(sorted(item.key for item in sections))
            self.checks.clear()
            self.view.setFrameSize_((self._width, max(len(sections), len(SECTIONS)) * 32))
            self.view.reloadData()
        for check in self.checks.values():
            check.setEnabled_(enabled)

    def toggle(self, index: int, visible: bool) -> None:
        """표시 선택을 바꾸되 수집·알림 설정에는 손대지 않는다."""
        if not self.enabled or not 0 <= index < len(self._tags):
            return
        key = self._tags[index]
        self.sections = tuple(
            replace(item, visible=visible) if item.key == key else item for item in self.sections
        )
        self._changed(self.sections)

    def move(self, key: str, insertion: int) -> bool:
        """나머지 영역의 상대 순서를 보존하며 잡은 행만 옮긴다."""
        if not self.enabled or not 0 <= insertion <= len(self.sections):
            return False
        positions = [i for i, item in enumerate(self.sections) if item.key == key]
        if not positions:
            return False
        previous = positions[0]
        target = insertion - 1 if insertion > previous else insertion
        if previous == target:
            return True
        sections = list(self.sections)
        sections.insert(target, sections.pop(previous))
        self.sections = tuple(sections)
        self.view.moveRowAtIndex_toIndex_(previous, target)
        self._changed(self.sections)
        return True
