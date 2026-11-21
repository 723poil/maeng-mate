"""표시용 문자열과 분리한 저장 칼럼의 구조를 검증한다."""

import math


def checked_raw_field(payload: object) -> dict[str, object]:
    """문자·수·선택 배열·사람 참조·기간을 원문 JSON 없이 제한한다."""
    if not isinstance(payload, dict) or set(payload) != {"type", "value"}:
        raise ValueError
    kind, value = payload["type"], payload["value"]
    if not isinstance(kind, str):
        raise ValueError
    if kind in {
        "title",
        "rich_text",
        "select",
        "status",
        "status_or_select",
        "url",
        "email",
        "phone_number",
        "created_time",
        "last_edited_time",
    }:
        valid = value is None or isinstance(value, str)
    elif kind == "number":
        valid = value is None or (
            type(value) in (int, float) and isinstance(value, (int, float)) and math.isfinite(value)
        )
    elif kind == "checkbox":
        valid = type(value) is bool
    elif kind in {"people", "multi_select"}:
        valid = isinstance(value, list) and all(isinstance(item, str) for item in value)
    elif kind == "date":
        valid = value is None or (
            isinstance(value, dict)
            and set(value) == {"kind", "start", "end"}
            and value["kind"] in {"date", "instant"}
            and isinstance(value["start"], str)
            and (value["end"] is None or isinstance(value["end"], str))
        )
    else:
        valid = False
    if not valid:
        raise ValueError
    return {"type": kind, "value": value}
