"""Notion 인증 정보의 비밀값·저장 포트·검증 후 교체 계약이다."""

import hmac
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal, Protocol

from src.application.credentials import (
    CredentialError,
    CredentialRecord,
    CredentialSecret,
    CredentialStore,
    validated_secret,
)
from src.application.notion_people import NotionDirectory, NotionPeople


@dataclass(frozen=True)
class NotionCredentialState:
    """인증 화면에 반환할 비밀값 없는 상태다."""

    revision: int
    configured: bool
    source: Literal["keychain", "environment", "none"]
    verification: Literal["unchecked", "ok", "failed"]
    restart_required: bool


class NotionConnectionChecker(Protocol):
    """토큰과 현재 수집 소스의 읽기 권한을 확인한다."""

    def verify(self, token: CredentialSecret) -> str | None:
        """성공하면 토큰 주인 ID(개인 토큰만)를 돌려주고 실패는 공개 가능한 분류로 알린다."""
        ...


class NotionCredentialsClient(Protocol):
    """화면이 저장된 비밀값을 조회하지 않고 관리하는 포트다."""

    def read(self, *, allow_prompt: bool = False) -> NotionCredentialState:
        """등록·확인·적용 상태만 반환한다."""
        ...

    def check(self, revision: int) -> NotionCredentialState:
        """현재 저장된 토큰의 연결을 확인한다."""
        ...

    def save(self, revision: int, token: CredentialSecret) -> NotionCredentialState:
        """새 토큰을 확인한 뒤에만 교체한다."""
        ...

    def delete(self, revision: int) -> NotionCredentialState:
        """토큰을 지우고 환경값 복귀를 막는다."""
        ...

    def people(self) -> NotionPeople:
        """저장된 토큰의 주인과 본인으로 고를 수 있는 사람 목록을 읽는다."""
        ...


class NotionCredentialsService:
    """외부 확인 중에는 잠금을 열어 두고 검증 뒤 조건부 교체한다."""

    def __init__(
        self,
        store: CredentialStore,
        checker: NotionConnectionChecker,
        *,
        legacy: CredentialSecret | None,
        active: CredentialSecret | None,
        directory: NotionDirectory | None = None,
        owner_found: Callable[[str], None] | None = None,
    ) -> None:
        """기존 환경값과 실제 실행에 주입한 비밀값은 서버 안에서만 보유한다."""
        self._store, self._checker = store, checker
        self._directory, self._owner_found = directory, owner_found
        self._legacy, self._active = legacy, active
        self._checked: tuple[int, Literal["ok", "failed"]] | None = None

    def selected(self, *, allow_prompt: bool = False) -> CredentialRecord:
        """삭제 표시를 미등록으로 오해해 예전 환경값을 되살리지 않는다."""
        return self._store.read(allow_prompt=allow_prompt) or CredentialRecord(0, self._legacy)

    def read(self, *, allow_prompt: bool = False) -> NotionCredentialState:
        """이번 실행의 실제 비밀값과 선택된 값이 다르면 재시작을 요구한다."""
        selected = self.selected(allow_prompt=allow_prompt)
        changed = (selected.token is None) != (self._active is None)
        if selected.token is not None and self._active is not None:
            changed = not hmac.compare_digest(selected.token.value, self._active.value)
        verification: Literal["unchecked", "ok", "failed"] = "unchecked"
        if self._checked is not None and self._checked[0] == selected.revision:
            verification = self._checked[1]
        return NotionCredentialState(
            selected.revision,
            selected.token is not None,
            "none"
            if selected.token is None
            else "keychain"
            if selected.revision
            else "environment",
            verification,
            changed,
        )

    def _current(self, revision: int) -> CredentialRecord:
        """확인 요청도 오래된 화면의 토큰을 대상으로 실행하지 않는다."""
        current = self.selected()
        if current.revision != revision:
            raise CredentialError("conflict")
        return current

    def check(self, revision: int) -> NotionCredentialState:
        """저장된 토큰만 확인하고 토큰·일반 설정은 변경하지 않는다."""
        current = self._current(revision)
        if current.token is None:
            raise CredentialError("missing")
        try:
            owner = self._checker.verify(current.token)
        except CredentialError:
            self._checked = (revision, "failed")
            raise
        self._current(revision)
        self._checked = (revision, "ok")
        self._fill_owner(owner)
        return self.read()

    def save(self, revision: int, token: CredentialSecret) -> NotionCredentialState:
        """검증 실패와 동시 수정에서 기존 정상 토큰을 보존한다."""
        self._current(revision)
        token = validated_secret(token.value)
        owner = self._checker.verify(token)
        saved = self._store.replace(revision, token)
        self._checked = (saved.revision, "ok")
        # 다시 시작 한 번으로 토큰과 본인 ID가 함께 적용되도록 저장 직후 채운다.
        self._fill_owner(owner)
        return self.read()

    def _fill_owner(self, owner: str | None) -> None:
        """개인 토큰의 주인을 본인 ID 후보로 알린다. 비어 있을 때만 채우는 것은 받는 쪽이 정한다."""
        if owner is not None and self._owner_found is not None:
            self._owner_found(owner)

    def people(self) -> NotionPeople:
        """선택된 토큰으로만 조회하고 토큰이 없으면 목록 대신 오류를 알린다."""
        token = self.selected().token
        if token is None:
            raise CredentialError("missing")
        if self._directory is None:
            raise CredentialError("unavailable")
        return self._directory.people(token)

    def delete(self, revision: int) -> NotionCredentialState:
        """비밀값 없는 삭제 표시만 남겨 예전 파일의 토큰을 차단한다."""
        self._current(revision)
        self._store.replace(revision, None)
        self._checked = None
        return self.read()


def credential_state_payload(value: NotionCredentialState) -> dict[str, object]:
    """토큰·해시·계정 정보 없이 관리 상태만 직렬화한다."""
    return {
        "revision": value.revision,
        "configured": value.configured,
        "source": value.source,
        "verification": value.verification,
        "restartRequired": value.restart_required,
    }


def parse_credential_state(value: object) -> NotionCredentialState:
    """인증 응답의 키·타입·허용 상태만 받아들인다."""
    if not isinstance(value, dict) or set(value) != {
        "revision",
        "configured",
        "source",
        "verification",
        "restartRequired",
    }:
        raise CredentialError("invalid")
    rev, configured, source, verification, restart = (
        value[key]
        for key in ("revision", "configured", "source", "verification", "restartRequired")
    )
    if type(rev) is not int or rev < 0 or type(configured) is not bool or type(restart) is not bool:
        raise CredentialError("invalid")
    if source not in ("keychain", "environment", "none") or verification not in (
        "unchecked",
        "ok",
        "failed",
    ):
        raise CredentialError("invalid")
    if configured != (source != "none"):
        raise CredentialError("invalid")
    return NotionCredentialState(
        rev,
        configured,
        "keychain"
        if source == "keychain"
        else "environment"
        if source == "environment"
        else "none",
        "ok" if verification == "ok" else "failed" if verification == "failed" else "unchecked",
        restart,
    )
