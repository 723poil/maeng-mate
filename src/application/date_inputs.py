"""개인 조회와 근무 기록이 공유하는 날짜·월 입력 계약이다."""

import calendar
import re
from datetime import date

from src.application.day_ports import DayInputError
from src.application.errors import InvalidDateError


def parse_day(value: str) -> date:
    """형식과 달력을 함께 검사하고 원문은 오류에 포함하지 않는다."""
    try:
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
            raise ValueError
        return date.fromisoformat(value)
    except ValueError:
        raise InvalidDateError() from None


def parse_month(value: str) -> tuple[date, date]:
    """정확한 YYYY-MM 월과 조회 범위를 검증한다."""
    if not re.fullmatch(r"\d{4}-\d{2}", value):
        raise DayInputError()
    try:
        year, month = map(int, value.split("-"))
        return date(year, month, 1), date(year, month, calendar.monthrange(year, month)[1])
    except ValueError:
        raise DayInputError() from None
