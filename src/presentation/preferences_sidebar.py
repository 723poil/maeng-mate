"""설정 기능을 이동하는 macOS 기본 사이드바다."""

from collections.abc import Callable
from enum import Enum

from AppKit import (
    NSColor,
    NSNoBorder,
    NSScrollView,
    NSTableCellView,
    NSTableColumn,
    NSTableView,
    NSTableViewSelectionHighlightStyleSourceList,
    NSTableViewStyleSourceList,
    NSView,
)
from Foundation import NSIndexSet, NSObject

from src.presentation.native_views import frame, label, symbol


class SettingsPage(Enum):
    """설정 페이지의 식별자·표시명·아이콘을 사이드바 표시 순서로 정의한다."""

    SCREEN = ("화면", "rectangle.grid.1x2")
    NOTIFICATIONS = ("알림", "bell")
    WORK_RULES = ("근무 규칙", "clock")
    CATEGORIES = ("카테고리", "tag")
    SOURCES = ("수집 소스", "square.stack")
    NOTION = ("Notion", "person.crop.square", True)
    SLACK = ("Slack", "bubble.left.and.bubble.right", True)
    MCP = ("MCP", "network", True)

    def __init__(self, title: str, icon: str, integration: bool = False) -> None:
        """연동 그룹에 속하는지와 네이티브 셀의 표시 정보를 보관한다."""
        self.title = title
        self.icon = icon
        self.integration = integration


class SidebarCell(NSTableCellView):
    """행을 누르면 창 이동 대신 기능을 선택한다."""

    def mouseDownCanMoveWindow(self) -> bool:
        """투명한 셀에서도 사이드바 선택을 우선한다."""
        return False


class SidebarActions(NSObject):
    """마우스와 키보드의 기본 목록 선택을 페이지 전환으로 연결한다."""

    owner: "PreferencesSidebar"

    def numberOfRowsInTableView_(self, table: NSTableView) -> int:
        """사용 가능한 설정 페이지 수를 반환한다."""
        return len(self.owner.rows)

    def tableView_viewForTableColumn_row_(
        self, table: NSTableView, column: NSTableColumn | None, row: int
    ) -> NSView | None:
        """선택 강조에 맞춰 글자·아이콘이 바뀌는 기본 셀을 만든다."""
        if not 0 <= row < len(self.owner.rows):
            return None
        item = self.owner.rows[row]
        if isinstance(item, str):
            cell = SidebarCell.alloc().initWithFrame_(frame(0, 0, 112, 24))
            text = label(cell, item, 8, 3, 96, size=11, bold=True, secondary=True)
            cell.setTextField_(text)
            return cell
        cell = SidebarCell.alloc().initWithFrame_(frame(0, 0, 124, 34))
        image = symbol(cell, item.icon, 8, 7.5)
        text = label(cell, item.title, 34, 7, 76, size=13)
        cell.setImageView_(image)
        cell.setTextField_(text)
        return cell

    def tableViewSelectionDidChange_(self, notification: object) -> None:
        """방향키 선택도 마우스와 동일하게 현재 페이지에 반영한다."""
        row = self.owner.view.selectedRow()
        if 0 <= row < len(self.owner.rows):
            item = self.owner.rows[row]
            if isinstance(item, SettingsPage):
                self.owner.changed(item)

    def tableView_isGroupRow_(self, table: NSTableView, row: int) -> bool:
        """연동·고급은 페이지가 아닌 그룹 제목으로 표시한다."""
        return isinstance(self.owner.rows[row], str)

    def tableView_shouldSelectRow_(self, table: NSTableView, row: int) -> bool:
        """그룹 제목을 클릭해도 내용 페이지를 바꾸지 않는다."""
        return isinstance(self.owner.rows[row], SettingsPage)

    def tableView_heightOfRow_(self, table: NSTableView, row: int) -> float:
        """그룹 제목과 선택 행의 높이를 구분한다."""
        return 24.0 if isinstance(self.owner.rows[row], str) else 34.0


class PreferencesSidebar:
    """페이지 전환만 담당하고 저장 설정과는 분리한다."""

    def __init__(
        self,
        parent: NSView,
        height: float,
        changed: Callable[[SettingsPage], None],
        *,
        pages: tuple[SettingsPage, ...],
    ) -> None:
        """활성화된 기능만 업무 설정과 연동 순서로 배치한다."""
        self.changed = changed
        primary = tuple(page for page in SettingsPage if page in pages and not page.integration)
        integrations = tuple(page for page in SettingsPage if page in pages and page.integration)
        self.rows: tuple[SettingsPage | str, ...] = (
            (*primary, "연동", *integrations) if integrations else primary
        )
        self._actions = SidebarActions.alloc().init()
        self._actions.owner = self
        # SourceList가 보태는 행 여백도 스크롤 뷰 경계 안에서 잘리도록 감싼다.
        self._scroll = NSScrollView.alloc().initWithFrame_(frame(8, 52, 132, height - 108))
        self._scroll.setDrawsBackground_(False)
        self._scroll.setBorderType_(NSNoBorder)
        self._scroll.setHasVerticalScroller_(False)
        self._scroll.setHasHorizontalScroller_(False)
        self.view = NSTableView.alloc().initWithFrame_(
            frame(0, 0, 132, sum(24 if isinstance(row, str) else 34 for row in self.rows))
        )
        column = NSTableColumn.alloc().initWithIdentifier_("page")
        column.setWidth_(112)
        self.view.addTableColumn_(column)
        self.view.setHeaderView_(None)
        self.view.setStyle_(NSTableViewStyleSourceList)
        self.view.setBackgroundColor_(NSColor.clearColor())
        self.view.setSelectionHighlightStyle_(NSTableViewSelectionHighlightStyleSourceList)
        self.view.setRowHeight_(34)
        self.view.setIntercellSpacing_((0, 0))
        self.view.setAllowsMultipleSelection_(False)
        self.view.setAllowsEmptySelection_(False)
        self.view.setDataSource_(self._actions)
        self.view.setDelegate_(self._actions)
        self._scroll.setDocumentView_(self.view)
        parent.addSubview_(self._scroll)
        self.select(SettingsPage.SCREEN)

    def select(self, page: SettingsPage) -> None:
        """설정 창 재사용 중에도 선택 상태를 기본 컨트롤에서 관리한다."""
        if page in self.rows:
            self.view.selectRowIndexes_byExtendingSelection_(
                NSIndexSet.indexSetWithIndex_(self.rows.index(page)), False
            )
