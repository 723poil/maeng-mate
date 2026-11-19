"""메뉴바와 MCP가 조회한 하루 근무 응답을 읽는다."""

from src.application.date_inputs import parse_day
from src.application.desktop import DayUpdate, DayView
from src.domain.daily_work import parse_clock, parse_leave, parse_rules
from src.infrastructure.api_decoding import boolean, instant, integer, json_object, strings, text


def day_view(value: object) -> DayView:
    """입력 날짜·버전·계산값을 검사하고 UI에 필요한 타입으로 변환한다."""
    raw = json_object(value)
    day = text(raw["date"])
    parse_day(day)
    arrival = raw["attendanceTime"]
    if arrival is not None:
        parse_clock(arrival)
    arrival_text = text(arrival) if arrival is not None else None
    required = raw["requiredMinutes"]
    return DayView(
        DayUpdate(
            day,
            integer(raw["revision"]),
            arrival_text,
            parse_leave(raw["leaveType"]),
            text(raw["plan"]),
        ),
        instant(raw["expectedDepartureAt"]),
        text(raw["calculationState"]),
        integer(required) if required is not None else None,
        integer(raw["lunchMinutes"]),
        boolean(raw["workRulesConfigured"]),
        strings(raw["warnings"]),
        parse_rules(raw.get("previewRules")),
        instant(raw.get("actualDepartureAt")),
        boolean(raw.get("overtime", False)),
    )
