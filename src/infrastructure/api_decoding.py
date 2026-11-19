"""로컬 API 응답에서 공통 값과 표시 칼럼을 검증한다."""

from datetime import datetime


def json_object(value: object) -> dict[str, object]:
    """외부 JSON의 문자열 키를 검사한 뒤 object 값으로 좁힌다."""
    if not isinstance(value, dict):
        raise ValueError
    result: dict[str, object] = {}
    for key, item in value.items():
        if not isinstance(key, str):
            raise ValueError
        result[key] = item
    return result


def text(value: object) -> str:
    """표시 문자열의 암묵적 변환을 금지한다."""
    if not isinstance(value, str):
        raise ValueError
    return value


def integer(value: object) -> int:
    """bool과 숫자 문자열을 개수나 버전으로 취급하지 않는다."""
    if type(value) is not int or value < 0:
        raise ValueError
    return value


def boolean(value: object) -> bool:
    """외부 상태값은 실제 bool만 허용한다."""
    if type(value) is not bool:
        raise ValueError
    return value


def instant(value: object) -> datetime | None:
    """시점은 시간대가 명시돼 있어야 한다."""
    if value is None:
        return None
    result = datetime.fromisoformat(text(value))
    if result.tzinfo is None:
        raise ValueError
    return result


def strings(value: object) -> tuple[str, ...]:
    """알림 코드를 정상 목록으로 가장하지 않도록 타입을 검사한다."""
    if not isinstance(value, list):
        raise ValueError
    return tuple(text(item) for item in value)


def parse_display_fields(value: object) -> tuple[tuple[str, str], ...] | None:
    """기본 표시와 직접 비워 둔 사용자 표시를 구분한다."""
    if value is None:
        return None
    if not isinstance(value, list):
        raise ValueError
    fields = []
    for raw in value:
        field = json_object(raw)
        fields.append((text(field["label"]), text(field["value"])))
    return tuple(fields)
