"""Notion 응답의 JSON 값을 검사해 안전한 오류 코드로 변환한다."""

from typing import cast

from src.infrastructure.notion.errors import SourceError


def record(value: object) -> dict[str, object]:
    """외부 JSON 객체의 컨테이너와 키를 검사한 뒤 타입을 좁힌다."""
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise SourceError("invalid_response")
    return cast(dict[str, object], value)


def array(value: object) -> list[object]:
    """검증되지 않은 JSON 배열을 object 목록으로 제한한다."""
    if not isinstance(value, list):
        raise SourceError("invalid_response")
    return cast(list[object], value)


def text(value: object) -> str:
    """외부 필드가 문자열인 경우에만 반환한다."""
    if not isinstance(value, str):
        raise SourceError("mapping_invalid")
    return value
