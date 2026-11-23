"""저장된 JSON을 검증한 후 업무 모델로 복원한다."""

from datetime import date, datetime
from typing import TypedDict, cast
from urllib.parse import urlsplit

from src.application.errors import QueryReadError
from src.domain.models import CalendarEvent, EventType, Leave, Period, SourceMapping, Task, WorkItem
from src.domain.raw_fields import checked_raw_field


class StoredItemIdentity(TypedDict):
    """조회 행을 만들 때 공통으로 복원하는 원천 정보다."""

    id: str
    source_id: str
    url: str
    source_edited_at: str
    observed_at: str


def record(value: object) -> dict[str, object]:
    """문자열 키를 가진 저장 JSON 객체만 허용한다."""
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise QueryReadError()
    return cast(dict[str, object], value)


def string(value: object) -> str:
    """타입 변환으로 손상된 자료를 숨기지 않는다."""
    if not isinstance(value, str):
        raise QueryReadError()
    return value


def nullable_string(payload: dict[str, object], key: str) -> str | None:
    """키 누락과 명시적인 null을 구별한다."""
    value = payload[key]
    return None if value is None else string(value)


def identities(value: object) -> tuple[str, ...]:
    """저장된 사람 목록의 모든 원소를 검사한다."""
    if not isinstance(value, list):
        raise QueryReadError()
    return tuple(string(part) for part in value)


def timestamp(value: str) -> datetime:
    """시간대 없는 시각을 서버의 로컬 시각으로 추측하지 않는다."""
    parsed = datetime.fromisoformat(value)
    if "T" not in value or parsed.tzinfo is None:
        raise QueryReadError()
    return parsed


def period(value: object) -> Period | None:
    """저장 구간의 종류·실제 날짜·끝값을 검증한다."""
    if value is None:
        return None
    payload = record(value)
    kind = payload["kind"]
    start = string(payload["start"])
    end = nullable_string(payload, "end")
    if kind == "date":
        first = date.fromisoformat(start)
        last = date.fromisoformat(end) if end is not None else first
        if (
            first.isoformat() != start
            or (end is not None and last.isoformat() != end)
            or last < first
        ):
            raise QueryReadError()
    elif kind == "instant":
        start_at = timestamp(start)
        if end is not None and timestamp(end) < start_at:
            raise QueryReadError()
    else:
        raise QueryReadError()
    return Period(kind, start, end)


def calendar_fields(
    payload: dict[str, object], source: SourceMapping
) -> tuple[str | None, EventType, bool]:
    """기존 payload의 기본값과 새 필드의 부분 누락을 구별한다."""
    keys = {"category", "event_type", "cancelled"}
    present = keys.intersection(payload)
    if not present and not {"category", "cancelled"}.intersection(source.properties):
        return None, "meeting", False
    if present != keys:
        raise QueryReadError()
    category = nullable_string(payload, "category")
    event_type = payload["event_type"]
    cancelled = payload["cancelled"]
    if event_type not in ("meeting", "leave", "other") or type(cancelled) is not bool:
        raise QueryReadError()
    return category, event_type, cancelled


def decode_item(value: object, source: SourceMapping) -> WorkItem:
    """소스와 항목 유형이 일치하는 정상 자료만 복원한다."""
    try:
        payload = record(value)
        if payload["source_id"] != source.source_id or payload["kind"] != source.kind:
            raise QueryReadError()
        common: StoredItemIdentity = {
            "id": string(payload["id"]),
            "source_id": string(payload["source_id"]),
            "url": string(payload["url"]),
            "source_edited_at": string(payload["source_edited_at"]),
            "observed_at": string(payload["observed_at"]),
        }
        if not common["id"] or urlsplit(common["url"]).scheme != "https":
            raise QueryReadError()
        timestamp(common["source_edited_at"])
        timestamp(common["observed_at"])
        display_values: dict[str, str] = {}
        stored_fields = record(payload.get("raw_fields", {}))
        raw_fields = {
            column.value_key: checked_raw_field(stored_fields[column.value_key])
            for column in source.display_columns or ()
            if column.value_key in stored_fields
        }
        if any(
            raw_fields[column.value_key]["type"] != column.kind
            for column in source.display_columns or ()
            if column.value_key in raw_fields
        ):
            raise QueryReadError()
        if source.display_columns:
            raw_values = record(payload.get("display_values", {}))
            display_values = {
                column.value_key: string(
                    raw_values[column.value_key]
                    if column.value_key in raw_values
                    else raw_values[column.property_id]
                )
                for column in source.display_columns
                if column.value_key in raw_values or column.property_id in raw_values
            }
        calendar_periods: dict[str, Period] = {}
        if source.calendar_column is not None:
            key = source.calendar_column.value_key
            raw_periods = record(payload.get("calendar_periods", {}))
            if key in raw_periods and (day := period(raw_periods[key])) is not None:
                calendar_periods[key] = day
        if source.kind == "task":
            return Task(
                **common,
                display_values=display_values,
                raw_fields=raw_fields,
                calendar_periods=calendar_periods,
                title=string(payload["title"]),
                assignee_ids=identities(payload["assignee_ids"]),
                status=nullable_string(payload, "status"),
                due=period(payload["due"]),
                parent_label=nullable_string(payload, "parent_label")
                if "parent_label" in payload
                else None,
                parent_order=nullable_string(payload, "parent_order")
                if "parent_order" in payload
                else None,
                qa_round=string(payload["qa_round"])
                if payload.get("qa_round") is not None
                else None,
            )
        if source.kind == "meeting":
            category, event_type, cancelled = calendar_fields(payload, source)
            return CalendarEvent(
                **common,
                display_values=display_values,
                raw_fields=raw_fields,
                calendar_periods=calendar_periods,
                title=string(payload["title"]),
                participant_ids=identities(payload["participant_ids"]),
                period=period(payload["period"]),
                category=category,
                event_type=event_type,
                cancelled=cancelled,
            )
        leave_period = period(payload["period"])
        approved = payload["approved"]
        if leave_period is None or type(approved) is not bool:
            raise QueryReadError()
        return Leave(
            **common,
            display_values=display_values,
            calendar_periods=calendar_periods,
            person_ids=identities(payload["person_ids"]),
            period=leave_period,
            approval=nullable_string(payload, "approval"),
            approved=approved,
        )
    except (KeyError, TypeError, ValueError):
        raise QueryReadError() from None
