"""작은 달력과 큰 달력에서 함께 사용하는 6주 월 격자다."""

from dataclasses import dataclass
from datetime import date, timedelta

from AppKit import (
    NSButton,
    NSColor,
    NSFont,
    NSLineBreakByTruncatingTail,
    NSTextAlignmentCenter,
    NSTextField,
    NSView,
)
from Foundation import NSObject

from src.domain.calendar import CALENDAR_GRID_DAYS, CalendarMonth, CalendarPreferences
from src.domain.work_dates import SEOUL
from src.presentation.calendar_controller import CalendarController
from src.presentation.calendar_style import (
    attendance_hint,
    attendance_symbol,
    box,
    item_summary,
    kind_color,
)
from src.presentation.native_views import button, frame, label, symbol

# 오늘·선택 날짜를 감싸는 원의 지름이다.
DAY_MARK = 22


def weekend_color(sunday: bool) -> NSColor:
    """일요일은 빨강, 토요일은 파랑이다."""
    return NSColor.systemRedColor() if sunday else NSColor.systemBlueColor()


class GridActions(NSObject):
    """셀 전체의 클릭을 한 날짜 선택으로 처리한다."""

    owner: "CalendarGrid"

    def day_(self, sender: NSButton) -> None:
        """오늘·주말·일정 칩 중 어디를 눌러도 같은 날짜를 선택한다."""
        self.owner.select_index(sender.tag())


@dataclass(frozen=True)
class _DayCell:
    """월을 넘겨도 같은 격자 위치에 남는 뷰와 입력 버튼이다."""

    contents: NSView
    number: NSTextField
    button: NSButton


class CalendarGrid:
    """작은 격자는 색 점, 큰 격자는 색상 일정 칩을 표시한다."""

    def __init__(
        self,
        parent: NSView,
        x: float,
        y: float,
        width: float,
        row_height: float,
        controller: CalendarController,
        *,
        compact: bool,
    ) -> None:
        """겹치지 않는 고정 격자를 만들어 월 이동의 높이를 유지한다."""
        self.controller = controller
        self.compact = compact
        self._cell_width = width / 7
        self._row_height = row_height
        self.view = NSView.alloc().initWithFrame_(frame(x, y, width, 20 + row_height * 6))
        parent.addSubview_(self.view)
        self._actions = GridActions.alloc().init()
        self._actions.owner = self
        self._month: CalendarMonth | None = None
        self._selected: date | None = None
        self._busy = False
        self._preferences = CalendarPreferences()
        self.buttons: dict[date, NSButton] = {}
        self._cells: list[_DayCell] = []
        cell_width = self._cell_width
        for column, title in enumerate(("일", "월", "화", "수", "목", "금", "토")):
            text = label(
                self.view,
                title,
                column * cell_width,
                row_height * 6 + 2,
                cell_width,
                size=11,
                secondary=True,
            )
            text.setAlignment_(NSTextAlignmentCenter)
            # 한국 달력처럼 일요일은 빨강, 토요일은 파랑으로 구분한다.
            if column in (0, 6):
                text.setTextColor_(weekend_color(column == 0))
        self._body = NSView.alloc().initWithFrame_(frame(0, 0, width, row_height * 6))
        self.view.addSubview_(self._body)
        # 선택 배경은 셀 뒤에서 이동하므로 날짜 선택만으로 일정 칩을 다시 만들지 않는다.
        # 작은 달력은 오늘 원과 같은 크기의 회색 원으로, 큰 달력은 칸 전체로 선택을 표시한다.
        self._selection = box(
            self._body,
            0,
            0,
            DAY_MARK if compact else cell_width - 2,
            DAY_MARK if compact else row_height - 2,
            NSColor.secondaryLabelColor().colorWithAlphaComponent_(0.22)
            if compact
            else NSColor.controlAccentColor().colorWithAlphaComponent_(0.10),
            DAY_MARK / 2 if compact else 6,
        )
        self._selection.setHidden_(True)

    def select_index(self, index: int) -> None:
        """월 자료와 일치하는 날짜만 선택한다."""
        data = self.controller.data
        if data is not None and 0 <= index < CALENDAR_GRID_DAYS:
            self.controller.select(data.first + timedelta(days=index))
            self.render(self._preferences)

    def _create_cell(self, index: int) -> _DayCell:
        """변하지 않는 격자선·날짜 라벨·클릭 영역을 한 번만 만든다."""
        width = self._cell_width
        row, column = divmod(index, 7)
        cell = NSView.alloc().initWithFrame_(
            frame(column * width, (5 - row) * self._row_height, width, self._row_height)
        )
        self._body.addSubview_(cell)
        cell.setWantsLayer_(True)
        layer = cell.layer()
        if layer is not None:
            layer.setMasksToBounds_(True)
        if not self.compact:
            color = NSColor.secondaryLabelColor().colorWithAlphaComponent_(0.13)
            box(cell, 0, 0, width, 1, color, 0)
            box(cell, 0, 0, 1, self._row_height, color, 0)
        # 날짜·일정 표시는 한 뷰에 두고, 투명 클릭 버튼은 항상 맨 위에 둔다.
        contents = NSView.alloc().initWithFrame_(frame(0, 0, width, self._row_height))
        cell.addSubview_(contents)
        number = label(
            contents,
            "1",
            (width - 22) / 2 if self.compact else 5,
            self._row_height - 24,
            22,
            size=11 if self.compact else 12,
        )
        number.setAlignment_(NSTextAlignmentCenter)
        overlay = button(cell, "", 0, 0, width, self._actions, "day:", bordered=False)
        overlay.setFrame_(frame(0, 0, width, self._row_height))
        overlay.setTransparent_(True)
        overlay.setTag_(index)
        overlay.setEnabled_(not self._busy)
        return _DayCell(contents, number, overlay)

    def _move_selection(self, data: CalendarMonth | None, selected: date | None) -> None:
        """선택일이 현재 격자 안에 있을 때만 배경을 해당 칸으로 옮긴다."""
        if data is None or selected is None or not data.first <= selected <= data.last:
            self._selection.setHidden_(True)
            return
        row, column = divmod((selected - data.first).days, 7)
        if self.compact:
            # 오늘은 이미 강조 원이 있으므로 선택 원을 겹쳐 그리지 않는다.
            self._selection.setHidden_(selected == data.today)
            self._selection.setFrame_(
                frame(
                    column * self._cell_width + (self._cell_width - DAY_MARK) / 2,
                    (5 - row) * self._row_height + self._row_height - 25,
                    DAY_MARK,
                    DAY_MARK,
                )
            )
            return
        self._selection.setFrame_(
            frame(
                column * self._cell_width + 1,
                (5 - row) * self._row_height + 1,
                self._cell_width - 2,
                self._row_height - 2,
            )
        )
        self._selection.setHidden_(False)

    def render(self, preferences: CalendarPreferences) -> None:
        """월·필터가 바뀌면 셀 내용을 갱신하고, 선택과 입력 잠금은 따로 반영한다."""
        data, selected = self.controller.data, self.controller.selected
        busy = self.controller.busy
        refresh = data is not self._month or preferences != self._preferences
        width = self._cell_width
        if data is not self._month or selected != self._selected:
            self._selected = selected
            self._move_selection(data, selected)
        if self._busy != busy:
            for slot in self._cells:
                slot.button.setEnabled_(not busy)
            self._busy = busy
        if not refresh:
            return
        self._month, self._preferences = data, preferences
        self._body.setHidden_(data is None)
        self.buttons.clear()
        if data is None:
            return
        if not self._cells:
            self._cells = [self._create_cell(index) for index in range(CALENDAR_GRID_DAYS)]
        for index, slot in enumerate(self._cells):
            day = data.first + timedelta(days=index)
            cell = slot.contents
            cell.setSubviews_([])
            today = day == data.today
            if today:
                box(
                    cell,
                    slot.number.frame()[0][0],
                    self._row_height - 25,
                    DAY_MARK,
                    DAY_MARK,
                    NSColor.controlAccentColor(),
                    DAY_MARK / 2,
                )
            size = 11 if self.compact else 12
            # 오늘 배경, 날짜 숫자, 근무 아이콘 순으로 겹치는 작은 칸도 같은 순서를 유지한다.
            cell.addSubview_(slot.number)
            slot.number.setStringValue_(str(day.day))
            slot.number.setFont_(
                NSFont.boldSystemFontOfSize_(size) if today else NSFont.systemFontOfSize_(size)
            )
            # 주말은 요일 머리와 같은 색, 다른 달 날짜는 같은 색을 흐리게 쓴다.
            color = (
                weekend_color(day.weekday() == 6) if day.weekday() >= 5 else NSColor.labelColor()
            )
            slot.number.setTextColor_(
                NSColor.whiteColor()
                if today
                else color.colorWithAlphaComponent_(0.4)
                if day.strftime("%Y-%m") != data.month
                else color
            )
            contents = self.controller.contents_on(day, preferences)
            record = contents.attendance
            if record is not None:
                name, tint = attendance_symbol(record)
                icon = symbol(cell, name, width - 17, self._row_height - 19)
                icon.setFrame_(frame(width - 17, self._row_height - 19, 12, 12))
                icon.setContentTintColor_(tint)
                if record.overtime:
                    extra = symbol(
                        cell,
                        "briefcase.fill" if day.weekday() >= 5 else "moon.fill",
                        width - 31,
                        self._row_height - 19,
                    )
                    extra.setFrame_(frame(width - 31, self._row_height - 19, 12, 12))
                    extra.setContentTintColor_(NSColor.systemOrangeColor())
            items = contents.items
            if self.compact:
                kinds = tuple(
                    dict.fromkeys(color for item in items for color in item.visible_colors)
                )
                dots = kinds[: max(0, int((width - 10) // 8))]
                for offset, (kind, source_color) in enumerate(dots):
                    box(
                        cell,
                        (width - len(dots) * 8 + 3) / 2 + offset * 8,
                        2,
                        5,
                        5,
                        kind_color(kind, source_color),
                        2.5,
                    )
            else:
                slots = 3 if self._row_height >= 80 else 2
                shown = items[: slots - 1] if len(items) > slots else items[:slots]
                for offset, item in enumerate(shown):
                    y = self._row_height - 43 - offset * 18
                    segment = (width - 8) / len(item.visible_colors)
                    for index, (kind, source_color) in enumerate(item.visible_colors):
                        box(
                            cell,
                            4 + index * segment,
                            y,
                            segment,
                            16,
                            kind_color(kind, source_color).colorWithAlphaComponent_(0.15),
                            3,
                        )
                    title = (
                        item.start_at.astimezone(SEOUL).strftime("%H:%M") + " "
                        if item.start_at and not item.all_day and item.first == day
                        else ""
                    ) + item.title
                    text = label(cell, title, 7, y + 1, width - 14, size=10)
                    text.setFont_(NSFont.systemFontOfSize_(10))
                    text.cell().setLineBreakMode_(NSLineBreakByTruncatingTail)
                if len(items) > slots:
                    label(
                        cell,
                        f"+{len(items) - slots + 1}개",
                        7,
                        self._row_height - 43 - (slots - 1) * 18,
                        width - 14,
                        size=10,
                        secondary=True,
                    )
            hints = [
                day.isoformat(),
                *(item_summary(item, day) for item in items[:6]),
            ]
            if record:
                hints.append(attendance_hint(record))
            slot.button.setToolTip_("\n".join(hints))
            slot.button.setAccessibilityLabel_(day.isoformat())
            self.buttons[day] = slot.button
