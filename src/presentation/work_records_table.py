"""같은 열 기준선으로 출근·휴가·실제 퇴근을 표시하는 네이티브 표다."""

from collections.abc import Callable
from datetime import date

from AppKit import (
    NSBackgroundStyleNormal,
    NSBezierPath,
    NSColor,
    NSNoBorder,
    NSScrollView,
    NSTableCellView,
    NSTableColumn,
    NSTableRowView,
    NSTableView,
    NSTableViewNoColumnAutoresizing,
    NSTableViewStylePlain,
    NSView,
)
from Foundation import NSIndexSet, NSObject

from src.application.work_records import WorkRecordItem
from src.application.workday import LEAVE_LABELS, extra_work_label
from src.domain.daily_work import LeaveType
from src.presentation.native_views import frame, label, place_label
from src.presentation.settings_badge import SettingsBadge

WEEKDAYS = ("월", "화", "수", "목", "금", "토", "일")
COLUMNS = (
    ("date", "날짜", 140.0),
    ("arrival", "출근", 76.0),
    ("leave", "휴가", 112.0),
    ("departure", "퇴근", 128.0),
)


class CalendarRowView(NSTableRowView):
    """선택 배경만 옅게 강조해 토·일 글자색과 일반 텍스트 대비를 유지한다."""

    def interiorBackgroundStyle(self) -> int:
        """선택 시 날짜 색이 흰색으로 바뀌지 않도록 일반 배경 스타일을 유지한다."""
        return NSBackgroundStyleNormal

    def drawSelectionInRect_(self, rect: tuple[tuple[float, float], tuple[float, float]]) -> None:
        """진한 파랑 대신 옅은 강조색으로 선택된 행의 범위만 표시한다."""
        NSColor.systemBlueColor().colorWithAlphaComponent_(0.12).setFill()
        (_, _), (width, height) = self.bounds()
        NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
            frame(4, 0, max(0, width - 8), height), 6, 6
        ).fill()


class RecordsTableActions(NSObject):
    """표의 행 선택을 날짜 선택 계약으로 연결한다."""

    owner: "WorkRecordsTable"

    def numberOfRowsInTableView_(self, table: NSTableView) -> int:
        """현재 조회된 월의 주말 포함 날짜 수를 반환한다."""
        return len(self.owner.items)

    def tableView_shouldSelectRow_(self, table: NSTableView, row: int) -> bool:
        """저장 중·미저장 변경이 있을 때 다른 날짜 선택을 막는다."""
        return self.owner.selectable and 0 <= row < len(self.owner.items)

    def tableView_rowViewForRow_(self, table: NSTableView, row: int) -> NSTableRowView:
        """기본 선택 동작은 유지하고 달력 색이 읽히는 행 강조만 제공한다."""
        return CalendarRowView.alloc().initWithFrame_(frame(0, 0, table.bounds()[1][0], 40))

    def tableViewSelectionDidChange_(self, notification: object) -> None:
        """키보드 선택도 마우스와 같은 날짜를 연다."""
        if self.owner._updating:
            return
        row = self.owner.view.selectedRow()
        if 0 <= row < len(self.owner.items):
            day = self.owner.items[row].day
            if day != self.owner.selected:
                self.owner.changed(day)

    def tableView_viewForTableColumn_row_(
        self, table: NSTableView, column: NSTableColumn | None, row: int
    ) -> NSView | None:
        """날짜는 오늘을 강조하고 숫자·미기록·휴가를 구분한다."""
        if column is None or not 0 <= row < len(self.owner.items):
            return None
        item = self.owner.items[row]
        key = column.identifier()
        width = column.width()
        cell = NSTableCellView.alloc().initWithFrame_(frame(0, 0, width, 40))
        cell.setWantsLayer_(True)
        layer = cell.layer()
        if layer is not None:
            layer.setMasksToBounds_(True)
        if key == "departure" and item.overtime:
            value = self.owner.cell_text(item, key)
            badge_width = 64.0 if item.day.weekday() >= 5 else 38.0
            text_width = max(0, width - 16 - badge_width - 6)
            text = label(cell, value, 8, 0, text_width, size=12, secondary=item.departure is None)
            place_label(text, 8, 0, text_width, 40)
            cell.setTextField_(text)
            badge = SettingsBadge(cell, width - 8 - badge_width, 10, badge_width)
            badge.update(extra_work_label(item.day), "orange")
        elif (
            key == "leave"
            and item.revision
            and item.leave not in {LeaveType.NONE, LeaveType.UNSELECTED}
        ):
            badge = SettingsBadge(cell, 6, 10, min(104, width - 12), align="left")
            badge.update(LEAVE_LABELS[item.leave], "blue")
        else:
            value = self.owner.cell_text(item, key)
            secondary = value in {"—", "미기록"}
            text = label(
                cell,
                value,
                8,
                0,
                width - 16,
                size=12,
                bold=key == "date" and item.day == self.owner.today,
                secondary=secondary,
            )
            place_label(text, 8, 0, width - 16, 40)
            if key == "date":
                if item.day.weekday() == 6:
                    text.setTextColor_(NSColor.systemRedColor())
                elif item.day.weekday() == 5 or item.day == self.owner.today:
                    text.setTextColor_(NSColor.systemBlueColor())
            cell.setTextField_(text)
        return cell


class WorkRecordsTable:
    """스크롤과 기본 선택 강조를 사용하는 월별 목록이다."""

    def __init__(
        self,
        parent: NSView,
        rect: tuple[tuple[float, float], tuple[float, float]],
        changed: Callable[[date], None],
    ) -> None:
        """열 너비와 행 높이를 고정해 선택·갱신 때 흔들리지 않게 한다."""
        self.changed = changed
        self.items: tuple[WorkRecordItem, ...] = ()
        self.selected: date | None = None
        self.today: date | None = None
        self.selectable = False
        self._updating = False
        self._actions = RecordsTableActions.alloc().init()
        self._actions.owner = self
        scroll = NSScrollView.alloc().initWithFrame_(rect)
        scroll.setHasVerticalScroller_(True)
        scroll.setHasHorizontalScroller_(False)
        scroll.setAutohidesScrollers_(True)
        scroll.setDrawsBackground_(False)
        scroll.setBorderType_(NSNoBorder)
        self.view = NSTableView.alloc().initWithFrame_(frame(0, 0, rect[1][0], rect[1][1]))
        # 자동 열 축소·기본 행 여백 때문에 배지가 밖으로 밀리지 않도록 표시 폭을 고정한다.
        self.view.setStyle_(NSTableViewStylePlain)
        self.view.setColumnAutoresizingStyle_(NSTableViewNoColumnAutoresizing)
        for key, title, width in COLUMNS:
            column = NSTableColumn.alloc().initWithIdentifier_(key)
            column.setTitle_(title)
            column.setWidth_(width)
            column.setMinWidth_(width)
            column.setMaxWidth_(width)
            self.view.addTableColumn_(column)
        self.view.setRowHeight_(40)
        self.view.setIntercellSpacing_((0, 0))
        self.view.setBackgroundColor_(NSColor.clearColor())
        self.view.setUsesAlternatingRowBackgroundColors_(True)
        self.view.setAllowsMultipleSelection_(False)
        self.view.setDataSource_(self._actions)
        self.view.setDelegate_(self._actions)
        scroll.setDocumentView_(self.view)
        parent.addSubview_(scroll)
        self._key: object = None

    def cell_text(self, item: WorkRecordItem, key: str) -> str:
        """실제 퇴근이 없으면 예상 시각 대신 미기록으로 표시한다."""
        if key == "date":
            return f"{item.day:%m.%d} {WEEKDAYS[item.day.weekday()]}" + (
                " · 오늘" if item.day == self.today else ""
            )
        if key == "arrival":
            return item.arrival or "—"
        if key == "departure":
            return item.departure or (
                "—" if not item.revision or item.leave == LeaveType.FULL else "미기록"
            )
        return (
            LEAVE_LABELS[item.leave]
            if item.revision
            else "휴무"
            if item.day.weekday() >= 5
            else "미기록"
        )

    def update(
        self,
        items: tuple[WorkRecordItem, ...],
        selected: date | None,
        today: date | None,
        selectable: bool,
    ) -> None:
        """같은 자료는 다시 그리지 않고 선택·스크롤 위치를 유지한다."""
        self.items, self.selected, self.today, self.selectable = items, selected, today, selectable
        key = (items, selected, today)
        if key == self._key:
            return
        self._key = key
        self._updating = True
        try:
            self.view.reloadData()
            for index, item in enumerate(items):
                if item.day == selected:
                    self.view.selectRowIndexes_byExtendingSelection_(
                        NSIndexSet.indexSetWithIndex_(index), False
                    )
                    break
            else:
                self.view.deselectAll_(None)
        finally:
            self._updating = False
