"""업무 설정의 공통 값과 공개 오류 코드를 검증한다."""


class WorkSettingsError(ValueError):
    """설정 원문 없이 입력 오류와 저장 충돌을 구분한다."""

    def __init__(self, code: str) -> None:
        """외부 값 대신 고정된 오류 코드만 보관한다."""
        self.code = code if code in {"invalid", "conflict", "missing"} else "storage"
        super().__init__(self.code)


def optional_strings(raw: object) -> tuple[str, ...] | None:
    """미지정과 빈 선택을 구분하는 목록이다."""
    if raw is None:
        return None
    if not isinstance(raw, list):
        raise ValueError
    items = tuple(nonblank(item) for item in raw)
    if len(items) != len(set(items)):
        raise ValueError
    return items


def nonblank(value: object) -> str:
    """외부 설정의 문자열을 확인한 뒤 타입을 좁힌다."""
    if not isinstance(value, str) or not value.strip():
        raise ValueError
    return value.strip()
