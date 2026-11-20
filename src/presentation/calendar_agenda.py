"""선택 날짜의 일정과 실제 근무 시각을 보여주는 읽기 전용 목록이다."""

from collections.abc import Callable

from AppKit import NSButton, NSColor, NSFont, NSScrollView, NSTextField, NSView
from Foundation import NSObject

from src.domain.calendar import CalendarDay, CalendarItem, CalendarPreferences
from src.presentation.calendar_controller import CalendarController
from src.presentation.calendar_style import (
    KIND_LABELS,
    attendance_hint,
    attendance_summary,
    attendance_symbol,
    box,
    item_time,
    kind_color,
)
from src.presentation.entry_list import status_badge
from src.presentation.native_views import button, frame, label, symbol


def agenda_height(item: CalendarItem) -> int:
    """직접 고른 칼럼이 있을 때만 상세를 위한 공간을 둔다."""
    if item.display_fields is not None:
        return 108 if item.display_fields else 76
    return 104 if item.kind in {"task", "qa"} else 76


class AgendaActions(NSObject):
    """원본 열기는 명시적 링크 버튼에서만 실행한다."""

    owner: "CalendarAgenda"

    def open_(self, sender: NSButton) -> None:
        """목록에서 확인한 항목의 검증된 URL만 전달한다."""
        index = sender.tag()
        if 0 <= index < len(self.owner.items):
            self.owner.open_item(self.owner.items[index].url)


class CalendarAgenda:
    """날짜의 일정이 많아져도 목록만 스크롤하고 근무는 아이콘으로 구분한다."""

    def __init__(
        self,
        scroll: NSScrollView,
        controller: CalendarController,
        open_item: Callable[[str | None], None],
    ) -> None:
        """표현에 필요한 선택 상태와 원본 링크 열기만 주입한다."""
        self.scroll, self.controller, self.open_item = scroll, controller, open_item
        self.items: tuple[CalendarItem, ...] = ()
        self._key: object = None
        self._actions = AgendaActions.alloc().init()
        self._actions.owner = self

    def render(self, preferences: CalendarPreferences) -> None:
        """같은 날짜·자료·필터에서는 스크롤 위치를 유지한다."""
        data, day = self.controller.data, self.controller.selected
        key = (data, day, preferences)
        if key == self._key:
            return
        self._key = key
        width = self.scroll.frame()[1][0]
        contents = self.controller.contents_on(day, preferences) if data and day else CalendarDay()
        self.items = contents.items
        record = contents.attendance
        height = max(
            self.scroll.frame()[1][1],
            sum(agenda_height(item) for item in self.items) + (68 if record else 0) + 16,
        )
        view = NSView.alloc().initWithFrame_(frame(0, 0, width, height))
        top = height - 8
        if record is not None:
            name, tint = attendance_symbol(record)
            icon = symbol(view, name, 8, top - 30)
            icon.setContentTintColor_(tint)
            label(
                view,
                attendance_summary(record),
                36,
                top - 29,
                width - 48,
                size=14,
                bold=True,
            )
            label(
                view,
                "퇴근 확정" if record.departure else "개인 근무 기록",
                36,
                top - 48,
                width - 48,
                size=11,
                secondary=True,
            )
            icon.setToolTip_(attendance_hint(record))
            top -= 68
        if not self.items:
            label(
                view,
                "표시할 일정 없음" if data else self.controller.error or "불러오는 중…",
                8,
                top - 24,
                width - 16,
                size=12,
                secondary=True,
            )
        for index, item in enumerate(self.items):
            task = item.display_fields is None and item.kind in {"task", "qa"}
            row_height = agenda_height(item)
            box(
                view,
                5,
                top - row_height + 13,
                3,
                row_height - 18,
                kind_color(item.kind, item.color),
                1,
            )
            heading = label(
                view,
                (
                    (item.category or KIND_LABELS[item.kind]) + " · "
                    if item.display_fields is None
                    else ""
                )
                + item_time(item, day or item.first),
                16,
                top - 20,
                width - 156 if task else width - 56,
                size=11,
                secondary=True,
            )
            if task:
                status_badge(view, item.status or "미지정", top - 23, x=width - 132, max_width=96)
            elif item.display_fields and item.badge_field_indices:
                # 시간과 원본 열기 버튼 사이에 맞는 배지만 놓는다. 전체 칼럼은 아래 상세에 남는다.
                x = 16 + heading.fittingSize()[0] + 6
                for field_index, (_, value) in enumerate(item.display_fields):
                    available = width - 34 - x
                    if available < 36:
                        break
                    if field_index in item.badge_field_indices and value:
                        x += (
                            status_badge(view, value, top - 23, x=x, max_width=min(128, available))
                            + 6
                        )
            title = NSTextField.wrappingLabelWithString_(item.title)
            title.setFont_(NSFont.systemFontOfSize_(13))
            title.setMaximumNumberOfLines_(2)
            title.setFrame_(frame(16, top - 60, width - 46, 36))
            title.setToolTip_(
                item.title
                + ("\n" + item.status if item.display_fields is None and item.status else "")
            )
            view.addSubview_(title)
            if item.display_fields:
                lines = tuple(f"{name} {value or '—'}" for name, value in item.display_fields)
                detail = NSTextField.wrappingLabelWithString_(" · ".join(lines))
                detail.setFont_(NSFont.systemFontOfSize_(11))
                detail.setTextColor_(NSColor.secondaryLabelColor())
                detail.setMaximumNumberOfLines_(2)
                detail.setFrame_(frame(16, top - 92, width - 35, 31))
                detail.setToolTip_("\n".join(lines))
                view.addSubview_(detail)
            if task:
                start = item.start_date.strftime("%Y.%m.%d") if item.start_date else "—"
                due = item.due_date.strftime("%Y.%m.%d") if item.due_date else "—"
                dates = label(
                    view,
                    f"시작 {start} · 기한 {due}",
                    16,
                    top - 83,
                    width - 28,
                    size=11,
                    secondary=True,
                )
                dates.setToolTip_(
                    "기간의 첫날은 시작일, 마지막 날은 기한입니다."
                    if item.start_date
                    else "날짜가 하나면 기한으로 표시합니다."
                )
            link = button(
                view, "↗", width - 28, top - 26, 24, self._actions, "open:", bordered=False
            )
            link.setTag_(index)
            link.setEnabled_(item.url is not None)
            link.setToolTip_("Notion 원본 열기")
            top -= row_height
        self.scroll.setDocumentView_(view)
        view.scrollPoint_((0, max(0, height - self.scroll.frame()[1][1])))
