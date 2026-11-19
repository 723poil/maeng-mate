"""연동 서비스와 앱 내부에서 공유하는 자격 정보 값·저장 계약이다."""

import re
from dataclasses import dataclass, field
from typing import Protocol


@dataclass(frozen=True)
class CredentialSecret:
    """객체 표현에 원문을 포함하지 않는 비밀값이다."""

    value: str = field(repr=False)


@dataclass(frozen=True)
class CredentialRecord:
    """None 토큰은 환경값으로 복귀하지 않는 명시적 삭제 표시다."""

    revision: int
    token: CredentialSecret | None = field(repr=False)


class CredentialError(Exception):
    """외부 응답·비밀값 대신 제한된 분류만 공개한다."""

    def __init__(self, code: str) -> None:
        """오류 원문이 공개 메시지로 섞이지 않게 한다."""
        allowed = {
            "invalid",
            "conflict",
            "keychain",
            "busy",
            "unauthorized",
            "permission",
            "unavailable",
            "timeout",
            "rate_limited",
            "missing",
        }
        self.code = code if code in allowed else "unavailable"
        super().__init__(self.code)


class CredentialStore(Protocol):
    """프로바이더의 보안 저장소를 교체할 수 있는 계약이다."""

    def read(self, *, allow_prompt: bool = False) -> CredentialRecord | None:
        """진짜 미등록만 None으로 반환하고 접근 실패는 구분한다."""
        ...

    def replace(self, expected_revision: int, token: CredentialSecret | None) -> CredentialRecord:
        """이전 버전의 저장·삭제를 거부한다."""
        ...


def validated_secret(value: object) -> CredentialSecret:
    """헤더 주입·공백·과도한 길이와 빈 토큰을 거부한다."""
    if not isinstance(value, str) or re.fullmatch(r"[\x21-\x7e]{32,1024}", value) is None:
        raise CredentialError("invalid")
    return CredentialSecret(value)
