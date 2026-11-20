"""두 달력 화면이 공유하는 인증된 월별 읽기 HTTP 어댑터다."""

from dataclasses import replace
from datetime import date

import httpx

from src.application.calendar import calendar_span
from src.application.errors import StatusReadError
from src.domain.calendar import (
    CALENDAR_GRID_DAYS,
    CALENDAR_KINDS,
    CalendarAttendance,
    CalendarItem,
    CalendarKind,
    CalendarMonth,
)
from src.domain.daily_work import parse_clock, parse_leave
from src.domain.source_display import SOURCE_COLORS
from src.infrastructure.api_decoding import (
    boolean,
    instant,
    json_object,
    parse_menu_display_fields,
    text,
)
from src.infrastructure.local_http import local_client, response_json


def _kind(value: object) -> CalendarKind:
    """허용한 종류만 선택해 외부 문자열을 타입으로 좁힌다."""
    kind = next((candidate for candidate in CALENDAR_KINDS if candidate == value), None)
    if kind is None or kind == "attendance":
        raise ValueError
    return kind


def parse_calendar(value: object) -> CalendarMonth:
    """날짜 범위·시점·중복과 응답 개수 제한을 확인한다."""
    raw = json_object(value)
    month = text(raw["month"])
    first, last = calendar_span(month)
    if raw["first"] != first.isoformat() or raw["last"] != last.isoformat():
        raise ValueError
    observed = instant(raw["asOf"])
    events, records = raw["items"], raw["attendance"]
    if (
        observed is None
        or not isinstance(events, list)
        or not isinstance(records, list)
        or len(events) > 1500
        or len(records) > CALENDAR_GRID_DAYS
    ):
        raise ValueError
    items: list[CalendarItem] = []
    keys: set[str] = set()

    def parse_item(event: object) -> CalendarItem:
        """각 표시 후보의 고유 키와 월 구간을 검증한다."""
        item = json_object(event)
        identifier = text(item["key"])
        start = date.fromisoformat(text(item["first"]))
        end = date.fromisoformat(text(item["last"]))
        if identifier in keys or not identifier or end < start or start > last or end < first:
            raise ValueError
        keys.add(identifier)
        url = item["url"]
        if url is not None:
            from src.application.notion_page_links import page_url

            url = page_url(url)
        color = item.get("color")
        if color is not None and (not isinstance(color, str) or color not in SOURCE_COLORS):
            raise ValueError
        category_key = item.get("categoryKey")
        if category_key is not None and (
            not isinstance(category_key, str) or not category_key.startswith("category:")
        ):
            raise ValueError
        fields, badge_indices = parse_menu_display_fields(item.get("displayFields"))
        return CalendarItem(
            identifier,
            _kind(item["kind"]),
            text(item["title"]),
            start,
            end,
            instant(item["startAt"]),
            instant(item["endAt"]),
            boolean(item["allDay"]),
            text(url) if url else None,
            text(item["status"]) if item["status"] is not None else None,
            date.fromisoformat(text(item["startDate"]))
            if item.get("startDate") is not None
            else None,
            date.fromisoformat(text(item["dueDate"])) if item.get("dueDate") is not None else None,
            color=color,
            category_key=category_key,
            category=text(item["category"]) if item.get("category") is not None else None,
            display_fields=fields,
            badge_field_indices=badge_indices,
        )

    for event in events:
        primary = parse_item(event)
        variants = json_object(event).get("alternatives", [])
        if not isinstance(variants, list):
            raise ValueError
        alternatives = tuple(parse_item(variant) for variant in variants)
        for variant in alternatives:
            if (variant.first, variant.last, variant.start_at, variant.end_at) != (
                primary.first,
                primary.last,
                primary.start_at,
                primary.end_at,
            ):
                raise ValueError
        items.append(replace(primary, alternatives=alternatives))
    attendance: list[CalendarAttendance] = []
    days: set[date] = set()
    for record in records:
        item = json_object(record)
        day = date.fromisoformat(text(item["date"]))
        arrival, departure = item["arrival"], item["departure"]
        if day in days or not first <= day <= last:
            raise ValueError
        days.add(day)
        for clock in (arrival, departure):
            if clock is not None:
                parse_clock(clock)
        attendance.append(
            CalendarAttendance(
                day,
                text(arrival) if arrival is not None else None,
                text(departure) if departure is not None else None,
                parse_leave(item["leave"]),
                boolean(item["overtime"]),
            )
        )
    return CalendarMonth(
        month,
        date.fromisoformat(text(raw["today"])),
        first,
        last,
        tuple(items),
        tuple(attendance),
        boolean(raw["stale"]),
        boolean(raw["truncated"]),
        boolean(raw["sourceConfigured"]),
        observed,
    )


class CalendarApi:
    """수집·근무 저장을 호출하지 않고 현재 월 자료만 읽는다."""

    def __init__(self, address: str, token: str) -> None:
        """검증한 루프백 주소·내부 관리 키만 주입한다."""
        self._address, self._token = address, token

    def month(self, month: str | None = None) -> CalendarMonth:
        """연결 실패를 빈 달력으로 바꾸지 않고 응답 범위를 검증한다."""
        try:
            with local_client(self._address, self._token, timeout=5) as client:
                response = client.get("/v1/calendar", params={"month": month} if month else None)
                response.raise_for_status()
                raw: object = response_json(response, max_bytes=2 * 1024 * 1024)
            value = parse_calendar(raw)
            if month is not None and value.month != month:
                raise ValueError
            return value
        except (ValueError, KeyError, TypeError, httpx.HTTPError):
            raise StatusReadError("캘린더 자료를 읽지 못했습니다. 다시 불러와 주세요.") from None
