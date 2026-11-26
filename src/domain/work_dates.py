"""업무 마감과 회의를 한국 날짜에 맞춰 분류한다."""

from __future__ import annotations

from datetime import date, datetime, time
from typing import TYPE_CHECKING
from uuid import UUID
from zoneinfo import ZoneInfo

if TYPE_CHECKING:
    from src.domain.models import Period

SEOUL = ZoneInfo("Asia/Seoul")


def seoul_datetime(instant: datetime) -> datetime | None:
    """시점의 서울 연도가 날짜 표현 범위를 벗어나면 현지 날짜를 만들지 않는다."""
    try:
        return instant.astimezone(SEOUL)
    except OverflowError:
        return None


def seoul_clock(instant: datetime) -> str:
    """서울 시·분을 표시하고 현지 날짜를 표현할 수 없으면 시간대를 명시한다."""
    local = seoul_datetime(instant)
    return local.strftime("%H:%M") if local else instant.strftime("%H:%M%z")


def canonical_identity(value: str) -> str:
    """UUID 표기만 통일하고 가상 사용자 ID는 그대로 보존한다."""
    try:
        return str(UUID(value))
    except ValueError:
        return value


def deadline_flags(
    period: Period | None, requested_day: date, observed_now: datetime
) -> tuple[bool, bool]:
    """날짜 마감은 다음 날부터, 시각 마감은 기준 시점부터 경과로 판단한다."""
    if period is None:
        return False, False
    deadline = period.end or period.start
    if period.kind == "date":
        day = date.fromisoformat(deadline)
        return day == requested_day, day < requested_day
    timestamp = datetime.fromisoformat(deadline).astimezone(SEOUL)
    local_now = observed_now.astimezone(SEOUL)
    reference = (
        local_now
        if requested_day == local_now.date()
        else datetime.combine(requested_day, time.min, SEOUL)
    )
    return timestamp.date() == requested_day, timestamp < reference


def meeting_on_day(period: Period | None, requested_day: date) -> bool:
    """종일 날짜는 마지막 날을 포함하고 시각 구간은 끝을 제외한다."""
    if period is None:
        return False
    if period.kind == "date":
        return (
            date.fromisoformat(period.start)
            <= requested_day
            <= date.fromisoformat(period.end or period.start)
        )
    start = datetime.fromisoformat(period.start).astimezone(SEOUL)
    end = datetime.fromisoformat(period.end).astimezone(SEOUL) if period.end else start
    if start == end:
        return start.date() == requested_day
    # 다음 날을 만들지 않아 최대 날짜에서도 끝 제외 규칙을 그대로 적용한다.
    return start.date() <= requested_day and (
        end.date() > requested_day or (end.date() == requested_day and end.time() > time.min)
    )
