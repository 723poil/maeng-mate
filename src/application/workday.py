"""근무 화면과 MCP 조회가 공유하는 표시값과 오늘 근무 요약이다."""

from datetime import date

from src.application.desktop import DayView
from src.domain.daily_work import LeaveType
from src.domain.work_dates import SEOUL

LEAVE_LABELS = {
    LeaveType.UNSELECTED: "미지정",
    LeaveType.NONE: "출근",
    LeaveType.FULL: "연차",
    LeaveType.AM_HALF: "오전 반차",
    LeaveType.PM_HALF: "오후 반차",
    LeaveType.AM_QUARTER: "오전 반반차",
    LeaveType.PM_QUARTER: "오후 반반차",
}
# 이전 기록의 미지정 값은 표시만 하고 새 입력의 선택지에서는 제외한다.
SELECTABLE_LEAVES = tuple(leave for leave in LEAVE_LABELS if leave != LeaveType.UNSELECTED)


def extra_work_label(day: date) -> str:
    """동일한 추가근무 체크를 평일 야근·주말근무로 구분해 표시한다."""
    return "주말근무" if day.weekday() >= 5 else "야근"


def workday_summary(day: DayView) -> dict[str, object]:
    """미기록·계산 불가를 그대로 두고 개인 메모와 규칙 원문은 제외한다."""
    return {
        "date": day.update.day,
        "timezone": "Asia/Seoul",
        "recorded": day.update.revision > 0,
        "attendanceTime": day.update.attendance,
        "overtime": day.overtime,
        "extraWorkLabel": extra_work_label(date.fromisoformat(day.update.day))
        if day.overtime
        else None,
        "actualDepartureAt": day.actual_departure.astimezone(SEOUL).isoformat()
        if day.actual_departure
        else None,
        "leaveType": day.update.leave.value,
        "leaveLabel": LEAVE_LABELS[day.update.leave],
        "expectedDepartureAt": day.departure.astimezone(SEOUL).isoformat()
        if day.departure
        else None,
        "calculationState": day.state,
        "requiredMinutes": day.required_minutes,
        "lunchMinutes": day.lunch_minutes,
        "workRulesConfigured": day.rules_configured,
        "warnings": list(day.warnings),
    }
