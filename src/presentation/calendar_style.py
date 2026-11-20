"""달력 종류별 색상·시간·근무 아이콘을 한 곳에서 정의한다."""

from datetime import date

from AppKit import NSBox, NSBoxCustom, NSColor, NSNoBorder, NSNoTitle, NSView

from src.application.workday import LEAVE_LABELS, extra_work_label
from src.domain.calendar import CalendarAttendance, CalendarItem, CalendarKind
from src.domain.daily_work import LeaveType
from src.domain.work_dates import SEOUL
from src.presentation.native_views import frame

KIND_LABELS: dict[CalendarKind, str] = {
    "meeting": "회의",
    "leave": "휴가",
    "other": "기타 일정",
    "task": "업무",
    "qa": "QA",
    "attendance": "출근 기록",
}


def kind_color(kind: CalendarKind, color: str | None = None) -> NSColor:
    """소스 색을 먼저 사용하고 기본값은 기존 종류별 색을 쓴다."""
    chosen = {
        "blue": NSColor.systemBlueColor,
        "green": NSColor.systemGreenColor,
        "orange": NSColor.systemOrangeColor,
        "purple": NSColor.systemPurpleColor,
        "red": NSColor.systemRedColor,
        "teal": NSColor.systemTealColor,
    }.get(color or "")
    if chosen is not None:
        return chosen()
    return {
        "meeting": NSColor.systemBlueColor,
        "leave": NSColor.systemGreenColor,
        "other": NSColor.secondaryLabelColor,
        "task": NSColor.systemPurpleColor,
        "qa": NSColor.systemOrangeColor,
        "attendance": NSColor.systemBlueColor,
    }[kind]()


def box(
    parent: NSView,
    x: float,
    y: float,
    width: float,
    height: float,
    color: NSColor,
    radius: float = 4,
) -> NSBox:
    """투명한 셀 안에 색상 표시를 만들고 프레임 밖으로 그리지 않는다."""
    view = NSBox.alloc().initWithFrame_(frame(x, y, width, height))
    view.setBoxType_(NSBoxCustom)
    view.setTitlePosition_(NSNoTitle)
    view.setBorderType_(NSNoBorder)
    view.setCornerRadius_(radius)
    view.setFillColor_(color)
    parent.addSubview_(view)
    return view


def item_time(item: CalendarItem, day: date) -> str:
    """종일·시점·다일 일정의 선택 날짜 시간만 표시한다."""
    if item.kind in {"task", "qa"} and item.due_date is not None:
        due_at = item.end_at or item.start_at
        return f"기한 {item.due_date:%m.%d}" + (
            f" {due_at.astimezone(SEOUL):%H:%M}" if not item.all_day and due_at else ""
        )
    if item.all_day:
        return "종일"
    if item.start_at is None:
        return "시간 미정"
    start = item.start_at.astimezone(SEOUL)
    value = start.strftime("%H:%M") if start.date() == day else "계속"
    if item.end_at is not None and item.end_at.astimezone(SEOUL).date() == day:
        value += "–" + item.end_at.astimezone(SEOUL).strftime("%H:%M")
    return value


def item_summary(item: CalendarItem, day: date) -> str:
    """날짜 칸의 도움말에 원본 일정 분류와 업무 상태·기한을 표시한다."""
    state = (
        " · " + item.status
        if item.display_fields is None and item.kind in {"task", "qa"} and item.status
        else ""
    )
    category = f"{item.category} · " if item.category and item.display_fields is None else ""
    return category + item_time(item, day) + state + "  " + item.title


def attendance_symbol(item: CalendarAttendance) -> tuple[str, NSColor]:
    """출근만 기록·퇴근 확정·개인 휴가를 서로 다른 아이콘으로 구분한다."""
    if item.departure is not None:
        return "checkmark.circle.fill", NSColor.systemGreenColor()
    if item.arrival is not None:
        return "clock.fill", NSColor.systemBlueColor()
    if item.leave == LeaveType.FULL:
        return "leaf.fill", NSColor.systemGreenColor()
    return "circle.dashed", NSColor.secondaryLabelColor()


def attendance_summary(item: CalendarAttendance) -> str:
    """저장된 출퇴근 시각을 보여 주고 시각이 없는 휴가는 이름으로 표시한다."""
    times = [
        f"{label} {value}"
        for label, value in (("출근", item.arrival), ("퇴근", item.departure))
        if value
    ]
    if times:
        return " · ".join(times)
    if item.leave not in (LeaveType.NONE, LeaveType.UNSELECTED):
        return LEAVE_LABELS[item.leave]
    return "출퇴근 시각 미기록"


def attendance_hint(item: CalendarAttendance) -> str:
    """아이콘 위에서 실제 시각과 개인 휴가·추가근무를 확인한다."""
    values = [
        f"출근 {item.arrival or '미기록'} · 퇴근 {item.departure or '미기록'}",
        LEAVE_LABELS[item.leave],
    ]
    if item.overtime:
        values.append(extra_work_label(item.day))
    return "\n".join(values)
