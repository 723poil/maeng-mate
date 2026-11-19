"""업무 실행 경계에서 공개할 수 있는 오류만 정의한다."""

import math


class SnapshotError(ValueError):
    """SQL과 원천 값을 숨기는 저장 실패다."""

    def __init__(self, code: str) -> None:
        """고정된 실패 분류만 보관한다."""
        self.code = code
        super().__init__(f"스냅샷 처리 실패: {code}")


class StatusReadError(ValueError):
    """로컬 조회 원문과 인증값을 숨기는 조회 실패다."""


class SettingsInputRejected(ValueError):
    """서버가 저장 전에 입력을 거절했으므로 같은 초안을 수정할 수 있다."""


class InvalidDateError(ValueError):
    """입력 원문 없이 날짜 형식 오류를 알린다."""

    def __init__(self) -> None:
        """고정된 안내만 공개한다."""
        super().__init__("날짜는 유효한 YYYY-MM-DD 형식이어야 합니다.")


class QueryReadError(ValueError):
    """원문·SQL·접속 정보 없이 업무 조회 실패를 알린다."""

    def __init__(self) -> None:
        """모든 저장 조회 실패를 안전한 메시지로 통일한다."""
        super().__init__("저장된 업무 자료를 조회할 수 없습니다.")


SOURCE_ERROR_CODES = frozenset(
    {
        "unavailable",
        "timeout",
        "rate_limited",
        "unauthorized",
        "permission_denied",
        "not_found",
        "request_invalid",
        "mapping_invalid",
        "invalid_response",
        "pagination_invalid",
    }
)


class SourceReadError(ValueError):
    """외부 원문 없이 고정 오류 분류와 안전한 대기 시간만 전달한다."""

    def __init__(self, code: str, *, retry_after: float | None = None) -> None:
        """회사 정보가 섞인 임의 오류 문자열은 계약 경계에서 거부한다."""
        if code not in SOURCE_ERROR_CODES:
            raise ValueError("지원하지 않는 원천 오류 분류입니다.")
        self.code = code
        self.retry_after = (
            retry_after
            if retry_after is not None and math.isfinite(retry_after) and retry_after >= 0
            else None
        )
        super().__init__(f"원천 조회 실패: {code}")
