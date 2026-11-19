"""MCP 조회 토큰의 공개 메타데이터와 만료 판정이다."""

from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True)
class McpAccessToken:
    """원문·해시를 포함하지 않는 접근 토큰 정보다."""

    id: str
    name: str
    created_at: float
    expires_at: float | None
    revoked_at: float | None = None
    last_used_at: float | None = None

    def status_at(self, now: float) -> Literal["active", "expired", "revoked"]:
        """폐기를 먼저 판정하고 만료 시각부터 접근을 막는다."""
        if self.revoked_at is not None:
            return "revoked"
        if self.expires_at is not None and self.expires_at <= now:
            return "expired"
        return "active"


@dataclass(frozen=True)
class McpAccessSnapshot:
    """조회 토큰 목록과 MCP 사용 선택의 일관된 읽기 결과다."""

    revision: int
    enabled: bool
    tokens: tuple[McpAccessToken, ...]
