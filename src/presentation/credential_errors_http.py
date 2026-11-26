"""프로바이더와 무관한 자격 정보 오류의 HTTP 공개 계약이다."""

from fastapi import HTTPException

from src.application.credentials import CredentialError


def credential_http_error(error: CredentialError) -> HTTPException:
    """공통 공개 코드를 유지하고 설정 오류·충돌·일시 장애를 구별한다."""
    # 외부 토큰의 거부는 로컬 API 관리자 인증 실패와 구별해 422로 전달한다.
    status = (
        409
        if error.code == "conflict"
        else 503
        if error.code in {"keychain", "busy", "unavailable", "timeout", "rate_limited"}
        else 422
    )
    return HTTPException(status, {"code": error.code})
