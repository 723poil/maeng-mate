"""작은 달력과 큰 달력에서 함께 사용하는 6주 월 격자다."""

from datetime import date, timedelta

from AppKit import (
    NSButton,
    NSColor,
    NSFont,
    NSLineBreakByTruncatingTail,
    NSTextAlignmentCenter,
    NSView,
)
from Foundation import NSObject

from src.domain.calendar import CALENDAR_GRID_DAYS, CalendarPreferences
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


class GridActions(NSObject):
    """셀 전체의 클릭을 한 날짜 선택으로 처리한다."""

    owner: "CalendarGrid"

    def day_(self, sender: NSButton) -> None:
        """오늘·주말·일정 칩 중 어디를 눌러도 같은 날짜를 선택한다."""
        self.owner.select_index(sender.tag())


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
        self._width = width
        self._row_height = row_height
        self.view = NSView.alloc().initWithFrame_(frame(x, y, width, 20 + row_height * 6))
        parent.addSubview_(self.view)
        self._actions = GridActions.alloc().init()
        self._actions.owner = self
        self._key: object = None
        self._busy = False
        self._preferences = CalendarPreferences()
        self.buttons: dict[date, NSButton] = {}

    def select_index(self, index: int) -> None:
        """월 자료와 일치하는 날짜만 선택한다."""
        data = self.controller.data
        if data is not None and 0 <= index < CALENDAR_GRID_DAYS:
            self.controller.select(data.first + timedelta(days=index))
            self.render(self._preferences)

    def render(self, preferences: CalendarPreferences) -> None:
        """날짜·자료·필터가 바뀔 때만 셀을 다시 그린다."""
        data, selected = self.controller.data, self.controller.selected
        self._preferences = preferences
        key = (data, selected, preferences)
        busy = self.controller.busy
        if key == self._key:
            if self._busy != busy:
                # 이전 월이 보이는 동안에는 클릭만 잠그고 42개 셀을 다시 만들지 않는다.
                for overlay in self.buttons.values():
                    overlay.setEnabled_(not busy)
                self._busy = busy
            return
        self._busy = busy
        self._key = key
        self.view.setSubviews_([])
        self.buttons = {}
        width = self._width / 7
        height = 20 + self._row_height * 6
        for column, title in enumerate(("일", "월", "화", "수", "목", "금", "토")):
            text = label(
                self.view, title, column * width, height - 18, width, size=11, secondary=True
            )
            text.setAlignment_(NSTextAlignmentCenter)
            if column in (0, 6):
                text.setTextColor_(
                    NSColor.systemRedColor() if column == 0 else NSColor.systemBlueColor()
                )
        if data is None:
            return
        for index in range(CALENDAR_GRID_DAYS):
            day = data.first + timedelta(days=index)
            row, column = divmod(index, 7)
            cell = NSView.alloc().initWithFrame_(
                frame(column * width, (5 - row) * self._row_height, width, self._row_height)
            )
            self.view.addSubview_(cell)
            cell.setWantsLayer_(True)
            layer = cell.layer()
            if layer is not None:
                layer.setMasksToBounds_(True)
            if day == selected:
                box(
                    cell,
                    1,
                    1,
                    width - 2,
                    self._row_height - 2,
                    NSColor.systemBlueColor().colorWithAlphaComponent_(0.10),
                    6,
                )
            if not self.compact:
                box(
                    cell,
                    0,
                    0,
                    width,
                    1,
                    NSColor.secondaryLabelColor().colorWithAlphaComponent_(0.13),
                    0,
                )
                box(
                    cell,
                    0,
                    0,
                    1,
                    self._row_height,
                    NSColor.secondaryLabelColor().colorWithAlphaComponent_(0.13),
                    0,
                )
            today = day == data.today
            if today:
                box(cell, 5, self._row_height - 25, 22, 22, NSColor.systemBlueColor(), 11)
            number = label(
                cell,
                str(day.day),
                5,
                self._row_height - 24,
                22,
                size=11 if self.compact else 12,
                bold=today,
            )
            number.setAlignment_(NSTextAlignmentCenter)
            color = (
                NSColor.systemRedColor()
                if day.weekday() == 6
                else NSColor.systemBlueColor()
                if day.weekday() == 5
                else NSColor.labelColor()
            )
            number.setTextColor_(
                NSColor.colorWithCalibratedWhite_alpha_(1, 1)
                if today
                else color.colorWithAlphaComponent_(0.45)
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
                kinds = tuple(dict.fromkeys((item.kind, item.color) for item in items))
                dots = kinds[: max(0, int((width - 10) // 8))]
                for offset, (kind, source_color) in enumerate(dots):
                    box(cell, 8 + offset * 8, 2, 5, 5, kind_color(kind, source_color), 2.5)
            else:
                slots = 3 if self._row_height >= 80 else 2
                shown = items[: slots - 1] if len(items) > slots else items[:slots]
                for offset, item in enumerate(shown):
                    y = self._row_height - 43 - offset * 18
                    box(
                        cell,
                        4,
                        y,
                        width - 8,
                        16,
                        kind_color(item.kind, item.color).colorWithAlphaComponent_(0.15),
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
            overlay = button(cell, "", 0, 0, width, self._actions, "day:", bordered=False)
            overlay.setFrame_(frame(0, 0, width, self._row_height))
            overlay.setTransparent_(True)
            overlay.setTag_(index)
            overlay.setEnabled_(not self.controller.busy)
            hints = [
                day.isoformat(),
                *(item_summary(item, day) for item in items[:6]),
            ]
            if record:
                hints.append(attendance_hint(record))
            overlay.setToolTip_("\n".join(hints))
            overlay.setAccessibilityLabel_(day.isoformat())
            self.buttons[day] = overlay
