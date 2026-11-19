"""프로바이더 등록·인증 배지의 문구와 색상을 통일한다."""

from typing import Literal


def registration_status(
    *,
    configured: bool,
    verification: Literal["unchecked", "ok", "failed"] = "unchecked",
    dirty: bool = False,
    busy: bool = False,
    error: bool = False,
    error_code: str | None = None,
) -> tuple[str, str]:
    """저장 여부와 인증 성공을 구별하고 진행·실패를 같은 표현으로 표시한다."""
    if busy:
        return "처리 중", "blue"
    if error:
        if error_code in {"timeout", "rate_limited"}:
            return ("응답 지연" if error_code == "timeout" else "요청 제한"), "orange"
        return "확인 실패", "red"
    if dirty:
        return "수정 중", "blue"
    if not configured:
        return "미등록", "neutral"
    if verification == "failed":
        return "확인 실패", "red"
    if verification == "ok":
        return "확인됨", "green"
    return "등록됨", "blue"
