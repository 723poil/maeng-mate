"""앱 내 MCP 상태·조회 전용 접근 발급과 관리 계약이다."""

import hashlib
import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Protocol
from uuid import uuid4

from src.application.credentials import CredentialSecret
from src.domain.mcp_access import McpAccessSnapshot, McpAccessToken


class McpAccessError(Exception):
    """입력·버전·저장 오류를 비밀값 없이 구분한다."""

    def __init__(self, code: str) -> None:
        """공개 가능한 오류 코드만 유지한다."""
        self.code = code if code in {"invalid", "conflict", "missing"} else "storage"
        super().__init__(self.code)


class McpAccessStore(Protocol):
    """원문 없이 토큰과 사용 상태를 관리하는 저장 포트다."""

    def read(self) -> McpAccessSnapshot:
        """설정과 토큰 목록을 같은 읽기에서 반환한다."""
        ...

    def set_enabled(self, revision: int, enabled: bool) -> McpAccessSnapshot:
        """같은 설정 버전일 때만 사용 여부를 바꾼다."""
        ...

    def insert(self, token: McpAccessToken, digest: str) -> None:
        """충분한 난수 토큰의 해시와 메타데이터만 보관한다."""
        ...

    def revoke(self, identifier: str, now: float) -> None:
        """토큰 원문 없이 지정한 접근을 즉시 폐기한다."""
        ...

    def authorize(self, digest: str, now: float) -> bool:
        """사용·만료·폐기를 함께 검사하고 성공한 사용 시각만 기록한다."""
        ...


@dataclass(frozen=True)
class McpSettingsState:
    """서버 준비와 사용 선택을 구분하는 설정 화면 상태다."""

    snapshot: McpAccessSnapshot
    ready: bool
    endpoint: str
    observed_at: float


@dataclass(frozen=True)
class IssuedMcpToken:
    """발급 요청에서만 한 번 전달하는 원문과 최신 관리 상태다."""

    state: McpSettingsState
    token: CredentialSecret = field(repr=False)
    id: str


class McpSettingsClient(Protocol):
    """UI에서만 사용하는 MCP 관리 포트다."""

    def read(self) -> McpSettingsState:
        """준비 상태·주소·토큰 메타데이터를 조회한다."""
        ...

    def set_enabled(self, revision: int, enabled: bool) -> McpSettingsState:
        """MCP 접근 사용 여부를 저장한다."""
        ...

    def issue(self, name: str, days: int | None) -> IssuedMcpToken:
        """지정한 이름·기한으로 조회 전용 토큰을 발급한다."""
        ...

    def revoke(self, identifier: str) -> McpSettingsState:
        """선택한 토큰을 즉시 폐기한다."""
        ...


class McpSettingsService:
    """앱 프로세스의 MCP 수명과 접근 메타데이터를 연결한다."""

    def __init__(
        self, store: McpAccessStore, endpoint: str, *, clock: Callable[[], float] = time.time
    ) -> None:
        """저장 구현·로컬 주소·시계를 주입한다."""
        self._store, self._endpoint, self._clock = store, endpoint, clock
        self.ready = False

    def read(self) -> McpSettingsState:
        """기본 설정 행을 만들지 않고 현재 준비 상태를 반환한다."""
        return McpSettingsState(self._store.read(), self.ready, self._endpoint, self._clock())

    def set_enabled(self, revision: int, enabled: bool) -> McpSettingsState:
        """전송 중인 요청은 마무리하고 이후 요청부터 선택을 적용한다."""
        self._store.set_enabled(revision, enabled)
        return self.read()

    def issue(self, name: str, days: int | None) -> IssuedMcpToken:
        """원문은 반환 후 복구할 수 없도록 해시만 저장한다."""
        name = " ".join(name.split())
        if not 1 <= len(name) <= 60 or (
            days is not None and (type(days) is not int or not 1 <= days <= 3650)
        ):
            raise McpAccessError("invalid")
        now = self._clock()
        token = CredentialSecret("mm_mcp_" + secrets.token_urlsafe(32))
        identifier = uuid4().hex
        self._store.insert(
            McpAccessToken(identifier, name, now, now + days * 86400 if days is not None else None),
            hashlib.sha256(token.value.encode()).hexdigest(),
        )
        return IssuedMcpToken(self.read(), token, identifier)

    def revoke(self, identifier: str) -> McpSettingsState:
        """폐기는 반복해도 이전 결과를 유지한다."""
        self._store.revoke(identifier, self._clock())
        return self.read()


def is_mcp_read_route(method: str, path: str) -> bool:
    """stdio 호환 토큰에도 지정한 메서드와 개인 조회 경로만 허용한다."""
    return (
        method == "GET" and path in {"/v1/status", "/v1/today", "/v1/day", "/v1/tasks/statuses"}
    ) or (method == "POST" and path == "/v1/tasks/search")
