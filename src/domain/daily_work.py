"""날짜별 개인 근무 기록과 교체 가능한 퇴근 계산이다."""

import re
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from enum import StrEnum
from itertools import pairwise
from typing import Protocol, TypeGuard

from src.domain.work_dates import SEOUL


class LeaveType(StrEnum):
    """원본 휴가와 구별해 사용자가 선택한 개인 계산 유형이다."""

    UNSELECTED = "unselected"
    NONE = "none"
    FULL = "full"
    AM_HALF = "am_half"
    PM_HALF = "pm_half"
    AM_QUARTER = "am_quarter"
    PM_QUARTER = "pm_quarter"


def parse_leave(value: object) -> LeaveType:
    """허용한 휴가 값만 통과시키고 임의 분류를 추측하지 않는다."""
    if not isinstance(value, str):
        raise ValueError("휴가 유형을 확인하세요.")
    return LeaveType(value)


def parse_clock(value: object) -> time:
    """분 단위 24시간 HH:MM만 받는다."""
    if not isinstance(value, str) or not re.fullmatch(r"(?:[01][0-9]|2[0-3]):[0-5][0-9]", value):
        raise ValueError("시간은 HH:MM 형식으로 입력하세요.")
    return time.fromisoformat(value)


def work_instant(day: date, clock: time | None) -> datetime | None:
    """입력한 서울 날짜·시각을 저장 시점으로 바꾸고 미기록은 그대로 둔다."""
    return datetime.combine(day, clock, SEOUL).astimezone(UTC) if clock is not None else None


OVERNIGHT_DEPARTURE_REASON = (
    "퇴근 예정이 다음 날이므로 확정할 수 없으며 자동 퇴근 알림을 보내지 않습니다."
)


def departure_on_record_day(expected: datetime | None, day: date) -> TypeGuard[datetime]:
    """야간 교대를 지원하지 않으므로 기록 당일의 퇴근 예정만 허용한다."""
    return expected is not None and expected.astimezone(SEOUL).date() == day


def departure_due(expected: datetime | None, day: date, now: datetime) -> TypeGuard[datetime]:
    """기록 당일의 예정 시각에 도달한 경우에만 실제 퇴근으로 확정할 수 있다."""
    return departure_on_record_day(expected, day) and expected <= now


@dataclass(frozen=True, order=True)
class BreakWindow:
    """하루 안에서 근무시간에서 제외할 고정 휴게 구간이다."""

    start: time
    end: time

    def __post_init__(self) -> None:
        """겹침은 규칙 전체에서 검사하고 여기서는 한 구간의 순서만 확인한다."""
        if self.start >= self.end:
            raise ValueError("휴게 종료 시각을 시작 시각보다 늦게 입력하세요.")


DEFAULT_WORKDAYS = (0, 1, 2, 3, 4)
MAX_EXTRA_BREAKS = 4


@dataclass(frozen=True)
class WorkRules:
    """시간 값과 선택한 계산 옵션을 날짜별 기록에 함께 저장한다."""

    daily_minutes: int
    half_minutes: int
    quarter_minutes: int
    lunch_start: time | None
    lunch_end: time | None
    arrival_start: time
    arrival_end: time
    workdays: tuple[int, ...] = DEFAULT_WORKDAYS
    count_early_arrival: bool = False
    early_half_departure: bool = True
    extra_breaks: tuple[BreakWindow, ...] = ()

    def __post_init__(self) -> None:
        """저장 전에 범위·요일·겹치는 휴게 구간을 검사한다."""
        # 화면의 하루 실근무 → 반차 → 반반차 순서로 고칠 입력을 알려 준다.
        if not 0 < self.daily_minutes <= 1440:
            raise ValueError("하루 실근무 시간은 1~1440분으로 입력하세요.")
        if not 0 < self.half_minutes < self.daily_minutes:
            raise ValueError("반차는 1분 이상, 하루 실근무 시간보다 짧게 입력하세요.")
        if not 0 < self.quarter_minutes < self.half_minutes:
            raise ValueError("반반차는 1분 이상, 반차보다 짧게 입력하세요.")
        if self.arrival_start > self.arrival_end:
            raise ValueError("출근 범위의 끝 시각을 시작 시각과 같거나 늦게 입력하세요.")
        if (self.lunch_start is None) != (self.lunch_end is None):
            raise ValueError("점심 시작과 종료를 함께 입력하세요.")
        if (
            not self.workdays
            or len(set(self.workdays)) != len(self.workdays)
            or any(type(day) is not int or not 0 <= day <= 6 for day in self.workdays)
        ):
            raise ValueError("근무 요일을 하나 이상 선택하세요.")
        if len(self.extra_breaks) > MAX_EXTRA_BREAKS:
            raise ValueError("추가 휴게는 최대 4개입니다.")
        windows = self.break_windows
        if any(left.end > right.start for left, right in pairwise(windows)):
            raise ValueError("겹치는 휴게 시간이 있습니다. 시작·종료 시각을 조정하세요.")

    @property
    def lunch(self) -> BreakWindow | None:
        """점심 예외를 추가 휴게와 구별한다."""
        if self.lunch_start is None or self.lunch_end is None:
            return None
        return BreakWindow(self.lunch_start, self.lunch_end)

    @property
    def break_windows(self) -> tuple[BreakWindow, ...]:
        """퇴근 예정이 밀릴 때 다음 휴게까지 순서대로 계산한다."""
        lunch = self.lunch
        return tuple(sorted((*self.extra_breaks, lunch) if lunch else self.extra_breaks))

    def payload(self) -> dict[str, object]:
        """계산 근거를 수정 이력과 함께 보존할 수 있는 JSON으로 반환한다."""
        return {
            "dailyMinutes": self.daily_minutes,
            "halfMinutes": self.half_minutes,
            "quarterMinutes": self.quarter_minutes,
            "lunchStart": self.lunch_start.strftime("%H:%M") if self.lunch_start else None,
            "lunchEnd": self.lunch_end.strftime("%H:%M") if self.lunch_end else None,
            "arrivalStart": self.arrival_start.strftime("%H:%M"),
            "arrivalEnd": self.arrival_end.strftime("%H:%M"),
            "workdays": list(self.workdays),
            "countEarlyArrival": self.count_early_arrival,
            "earlyHalfDeparture": self.early_half_departure,
            "extraBreaks": [
                {"start": window.start.strftime("%H:%M"), "end": window.end.strftime("%H:%M")}
                for window in self.extra_breaks
            ],
        }


# 처음 실행할 때는 입력한 출근 시각을 사용하고, 고정 휴게는 사용자가 지정한다.
DEFAULT_WORK_RULES = WorkRules(
    daily_minutes=480,
    half_minutes=240,
    quarter_minutes=120,
    lunch_start=None,
    lunch_end=None,
    arrival_start=time(0),
    arrival_end=time(23, 59),
    early_half_departure=False,
)


def parse_breaks(value: object) -> tuple[BreakWindow, ...]:
    """휴게의 시작·종료 두 항목만 읽는다."""
    if not isinstance(value, list) or len(value) > MAX_EXTRA_BREAKS:
        raise ValueError("추가 휴게는 최대 4개입니다.")
    windows = []
    for window in value:
        if not isinstance(window, dict) or set(window) != {"start", "end"}:
            raise ValueError("휴게 구간을 확인하세요.")
        windows.append(BreakWindow(parse_clock(window["start"]), parse_clock(window["end"])))
    return tuple(sorted(windows))


def parse_rules(value: object) -> WorkRules | None:
    """개인 설정과 과거 기록을 같은 방식으로 읽고, 이전 형식은 당시 기본값을 유지한다."""
    if value is None:
        return None
    keys = {
        "dailyMinutes",
        "halfMinutes",
        "quarterMinutes",
        "lunchStart",
        "lunchEnd",
        "arrivalStart",
        "arrivalEnd",
    }
    options = {"workdays", "countEarlyArrival", "earlyHalfDeparture", "extraBreaks"}
    if not isinstance(value, dict) or not keys <= set(value) or set(value) - keys - options:
        raise ValueError("근무 규칙 키를 확인하세요.")
    daily, half, quarter = value["dailyMinutes"], value["halfMinutes"], value["quarterMinutes"]
    if type(daily) is not int or type(half) is not int or type(quarter) is not int:
        raise ValueError("근무·휴가 시간은 소수점 없이 분 단위로 입력하세요.")
    workdays = value.get("workdays", list(DEFAULT_WORKDAYS))
    count_early = value.get("countEarlyArrival", False)
    early_half = value.get("earlyHalfDeparture", True)
    if (
        not isinstance(workdays, list)
        or any(type(day) is not int for day in workdays)
        or type(count_early) is not bool
        or type(early_half) is not bool
    ):
        raise ValueError("근무 요일과 계산 옵션을 확인하세요.")
    lunch_start, lunch_end = value["lunchStart"], value["lunchEnd"]
    return WorkRules(
        daily,
        half,
        quarter,
        parse_clock(lunch_start) if lunch_start is not None else None,
        parse_clock(lunch_end) if lunch_end is not None else None,
        parse_clock(value["arrivalStart"]),
        parse_clock(value["arrivalEnd"]),
        tuple(sorted(workdays)),
        count_early,
        early_half,
        parse_breaks(value.get("extraBreaks", [])),
    )


@dataclass(frozen=True)
class DayRecord:
    """수동 입력과 계산 당시 규칙을 서울 날짜별로 보존한다."""

    day: date
    revision: int = 0
    attendance: datetime | None = None
    leave: LeaveType = LeaveType.NONE
    plan: str = ""
    rules: WorkRules | None = None
    updated_at: datetime | None = None
    checkout_at: datetime | None = None
    overtime: bool = False

    @property
    def attendance_warnings(self) -> tuple[str, ...]:
        """휴가 입력 안내와 근무일 출근 범위 경고를 기록에서 판단한다."""
        if self.leave in {LeaveType.AM_HALF, LeaveType.AM_QUARTER}:
            return ("morning_leave_uses_actual_arrival",)
        if (
            self.leave == LeaveType.NONE
            and self.attendance is not None
            and self.rules is not None
            and self.day.weekday() in self.rules.workdays
            and not self.rules.arrival_start
            <= self.attendance.astimezone(SEOUL).time()
            <= self.rules.arrival_end
        ):
            return ("outside_normal_arrival_window",)
        return ()


@dataclass(frozen=True)
class DepartureResult:
    """계산 불가 이유를 시각과 분리해 표시한다."""

    state: str
    departure: datetime | None = None
    required_minutes: int | None = None
    lunch_minutes: int = 0
    warnings: tuple[str, ...] = ()


class DeparturePolicy(Protocol):
    """회사별 계산 절차를 교체하는 순수 전략 계약이다."""

    def calculate(self, record: DayRecord) -> DepartureResult:
        """저장된 입력과 규칙만으로 예정 시각 또는 미계산 근거를 반환한다."""
        ...


class FlexibleDeparturePolicy:
    """휴가 차감과 무급 점심시간을 반영해 퇴근 예정을 계산한다."""

    def calculate(self, record: DayRecord) -> DepartureResult:
        """입력을 보존하고 오후 반차의 이른 퇴근 기준과 고정 점심을 반영한다."""
        workdays = record.rules.workdays if record.rules else DEFAULT_WORKDAYS
        if record.day.weekday() not in workdays:
            state = "weekend_manual" if record.day.weekday() >= 5 else "non_workday"
            return DepartureResult(state)
        if record.leave == LeaveType.UNSELECTED:
            return DepartureResult("leave_unselected")
        if record.leave == LeaveType.FULL:
            return DepartureResult("full_leave", required_minutes=0)
        if record.rules is None:
            return DepartureResult("rules_missing")
        if record.attendance is None:
            return DepartureResult("attendance_missing")
        rules = record.rules
        deduction = (
            rules.half_minutes
            if record.leave in {LeaveType.AM_HALF, LeaveType.PM_HALF}
            else rules.quarter_minutes
            if record.leave in {LeaveType.AM_QUARTER, LeaveType.PM_QUARTER}
            else 0
        )
        required = rules.daily_minutes - deduction
        arrival = record.attendance.astimezone(SEOUL)
        earliest_start = datetime.combine(record.day, rules.arrival_start, SEOUL)
        start = arrival if rules.count_early_arrival else max(arrival, earliest_start)
        finish = start + timedelta(minutes=required)
        lunch_minutes = 0
        lunch = rules.lunch
        for window in rules.break_windows:
            break_start = datetime.combine(record.day, window.start, SEOUL)
            break_end = datetime.combine(record.day, window.end, SEOUL)
            # 기본 오후 반차는 점심 종료 정각부터 휴게를 반영한다.
            if (
                rules.early_half_departure
                and record.leave == LeaveType.PM_HALF
                and window == lunch
                and finish < break_end
            ):
                continue
            if start < break_end and finish > break_start:
                minutes = int((break_end - max(start, break_start)).total_seconds() // 60)
                if window == lunch:
                    lunch_minutes += minutes
                finish += timedelta(minutes=minutes)
        # 출근과 계산 시각은 보존하되 당일 확정·알림이 불가능한 상태를 구별한다.
        state = "ready" if departure_on_record_day(finish, record.day) else "overnight"
        return DepartureResult(state, finish, required, lunch_minutes, record.attendance_warnings)
