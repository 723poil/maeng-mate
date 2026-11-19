"""Slack 개인 수신자와 봇 인증을 독립 저장하는 설정 유스케이스다."""

from dataclasses import dataclass, field, replace
from threading import Lock
from typing import Literal, Protocol

from src.application.credentials import (
    CredentialError,
    CredentialRecord,
    CredentialSecret,
    CredentialStore,
    validated_secret,
)
from src.application.runtime_settings import (
    RuntimeSettingsConflictError,
    RuntimeSettingsStore,
    parse_slack_user,
)
from src.domain.runtime_settings import RuntimeSettings, SlackSettings


@dataclass(frozen=True)
class SlackIdentity:
    """인증된 봇의 표시 이름만 보유하고 ID·토큰은 반환하지 않는다."""

    workspace: str | None = field(default=None, repr=False)
    bot: str | None = field(default=None, repr=False)


@dataclass(frozen=True)
class SlackSettingsState:
    """보안 입력을 되채우지 않는 설정 상태다."""

    revision: int
    credential_revision: int
    user_id: str | None = field(repr=False)
    configured: bool
    verification: Literal["unchecked", "ok", "failed"] = "unchecked"
    identity: SlackIdentity = SlackIdentity()
    app_revision: int = 0
    app_configured: bool = False


class SlackTestError(Exception):
    """발송 실패와 전송 결과를 모르는 상태를 원문 없이 구분한다."""

    def __init__(self, code: Literal["recipient", "unverified", "delivery_unknown"]) -> None:
        """API와 화면에 허용된 오류 분류만 전달한다."""
        self.code = code
        super().__init__(code)


class SlackTestSender(Protocol):
    """개인 테스트 발송 구현을 설정 저장과 분리한다."""

    def send_test(self, token: CredentialSecret, user_id: str) -> None:
        """저장된 본인에게 고정 테스트 메시지 한 건만 요청한다."""
        ...


class SlackAuthChecker(Protocol):
    """실제 메시지·DM 생성 없이 봇 인증만 확인한다."""

    def verify(self, token: CredentialSecret) -> SlackIdentity:
        """성공한 봇 인증만 반환하고 실패는 제한된 코드로 알린다."""
        ...


def validated_app_token(value: object) -> CredentialSecret:
    """앱 토큰 형식만 검사하며 연결 성공으로 해석하지 않는다."""
    token = validated_secret(value)
    if not token.value.startswith("xapp-"):
        raise CredentialError("invalid")
    return token


class SlackSettingsClient(Protocol):
    """화면에서 비밀값을 다시 읽지 않고 설정을 관리하는 계약이다."""

    def read(self, *, allow_prompt: bool = False) -> SlackSettingsState:
        """등록 상태와 본인 ID만 읽는다."""
        ...

    def save_member(self, revision: int, user_id: str | None) -> SlackSettingsState:
        """일반 설정의 같은 버전에 수신자만 저장한다."""
        ...

    def save_token(self, revision: int, token: CredentialSecret) -> SlackSettingsState:
        """후보 토큰 인증 성공 뒤 Keychain에만 저장한다."""
        ...

    def check(self, revision: int) -> SlackSettingsState:
        """현재 저장된 토큰의 인증만 확인한다."""
        ...

    def delete_token(self, revision: int) -> SlackSettingsState:
        """수신자 설정은 유지하고 토큰만 삭제한다."""
        ...

    def save_app_token(self, revision: int, token: CredentialSecret) -> SlackSettingsState:
        """Socket Mode를 시작하지 않고 앱 토큰만 보관한다."""
        ...

    def delete_app_token(self, revision: int) -> SlackSettingsState:
        """봇·멤버 설정을 유지하고 앱 토큰만 삭제한다."""
        ...

    def test_notification(self, revision: int, credential_revision: int) -> SlackSettingsState:
        """저장된 멤버와 인증된 봇으로 개인 테스트 알림을 보낸다."""
        ...


class SlackSettingsService:
    """외부 인증 중 저장 잠금을 잡지 않고 각 저장소의 버전을 검증한다."""

    def __init__(
        self,
        runtime: RuntimeSettingsStore,
        active: RuntimeSettings,
        credentials: CredentialStore,
        checker: SlackAuthChecker,
        app_credentials: CredentialStore,
        test_sender: SlackTestSender,
    ) -> None:
        """일반 설정·보안 저장·인증 포트를 시작 경계에서 주입한다."""
        self._runtime, self._active, self._credentials, self._checker = (
            runtime,
            active,
            credentials,
            checker,
        )
        self._app_credentials = app_credentials
        self._test_sender = test_sender
        self._test_lock = Lock()
        self._checked: tuple[int, SlackIdentity | None] | None = None

    def _credential(
        self, revision: int | None = None, *, allow_prompt: bool = False
    ) -> CredentialRecord:
        """환경값 복귀 없이 Keychain 상태를 읽고 요청 버전을 확인한다."""
        current = self._credentials.read(allow_prompt=allow_prompt) or CredentialRecord(0, None)
        if revision is not None and current.revision != revision:
            raise CredentialError("conflict")
        return current

    def read(self, *, allow_prompt: bool = False) -> SlackSettingsState:
        """읽기로 초기 행을 만들거나 토큰 원문을 반환하지 않는다."""
        runtime = self._runtime.read() or self._active
        credential = self._credential(allow_prompt=allow_prompt)
        app = self._app_credentials.read(allow_prompt=allow_prompt) or CredentialRecord(0, None)
        verification: Literal["unchecked", "ok", "failed"] = "unchecked"
        identity = SlackIdentity()
        if (
            self._checked is not None
            and self._checked[0] == credential.revision
            and credential.token is not None
        ):
            verification = "ok" if self._checked[1] is not None else "failed"
            identity = self._checked[1] or identity
        return SlackSettingsState(
            runtime.revision,
            credential.revision,
            runtime.slack.user_id,
            credential.token is not None,
            verification,
            identity,
            app.revision,
            app.token is not None,
        )

    def save_member(self, revision: int, user_id: str | None) -> SlackSettingsState:
        """다른 프로바이더·일반 값을 보존해 수신자 필드만 원자 저장한다."""
        try:
            user_id = parse_slack_user(user_id)
        except ValueError:
            raise CredentialError("invalid") from None
        current = self._runtime.read() or self._active
        if current.revision != revision:
            raise CredentialError("conflict")
        try:
            self._runtime.save(replace(current, slack=SlackSettings(user_id)))
        except RuntimeSettingsConflictError:
            raise CredentialError("conflict") from None
        return self.read()

    def save_token(self, revision: int, token: CredentialSecret) -> SlackSettingsState:
        """새 토큰이 유효하지 않으면 기존 토큰을 교체하지 않는다."""
        self._credential(revision)
        token = validated_secret(token.value)
        identity = self._checker.verify(token)
        saved = self._credentials.replace(revision, token)
        self._checked = (saved.revision, identity)
        return self.read()

    def check(self, revision: int) -> SlackSettingsState:
        """입력 칸이 비어 있어도 현재 등록된 토큰을 검사한다."""
        current = self._credential(revision)
        if current.token is None:
            raise CredentialError("missing")
        try:
            identity = self._checker.verify(current.token)
        except CredentialError:
            self._checked = (revision, None)
            raise
        self._credential(revision)
        self._checked = (revision, identity)
        return self.read()

    def delete_token(self, revision: int) -> SlackSettingsState:
        """명시적 삭제는 수신자와 다른 계정 설정을 지우지 않는다."""
        self._credential(revision)
        self._credentials.replace(revision, None)
        self._checked = None
        return self.read()

    def save_app_token(self, revision: int, token: CredentialSecret) -> SlackSettingsState:
        """형식 검사 후 앱 전용 Keychain 버전만 교체한다."""
        self._app_credentials.replace(revision, validated_app_token(token.value))
        return self.read()

    def delete_app_token(self, revision: int) -> SlackSettingsState:
        """독립 버전으로 앱 토큰만 지우며 외부 연결은 만들지 않는다."""
        self._app_credentials.replace(revision, None)
        return self.read()

    def test_notification(self, revision: int, credential_revision: int) -> SlackSettingsState:
        """UI의 활성 조건을 신뢰하지 않고 저장된 버전·인증을 재검사한다."""
        if not self._test_lock.acquire(blocking=False):
            raise CredentialError("busy")
        try:
            state = self.read()
            if state.revision != revision or state.credential_revision != credential_revision:
                raise CredentialError("conflict")
            if state.user_id is None:
                raise SlackTestError("recipient")
            if state.verification != "ok":
                raise SlackTestError("unverified")
            credential = self._credential(credential_revision)
            if credential.token is None:
                raise CredentialError("missing")
            try:
                self._test_sender.send_test(credential.token, state.user_id)
            except CredentialError as error:
                if error.code == "unauthorized":
                    self._checked = (credential_revision, None)
                raise
            # 발송 성공 뒤 추가 저장소 조회 실패를 발송 실패로 오인하지 않게 한다.
            return state
        finally:
            self._test_lock.release()


def slack_state_payload(value: SlackSettingsState) -> dict[str, object]:
    """토큰·해시·Keychain 원문 없이 설정 화면용 상태만 직렬화한다."""
    return {
        "revision": value.revision,
        "credentialRevision": value.credential_revision,
        "userId": value.user_id,
        "configured": value.configured,
        "verification": value.verification,
        "workspace": value.identity.workspace,
        "bot": value.identity.bot,
        "appRevision": value.app_revision,
        "appConfigured": value.app_configured,
    }


def parse_slack_state(value: object) -> SlackSettingsState:
    """로컬 응답도 고정된 키와 타입·등록 상태를 검사한다."""
    if not isinstance(value, dict) or set(value) != {
        "revision",
        "credentialRevision",
        "userId",
        "configured",
        "verification",
        "workspace",
        "bot",
        "appRevision",
        "appConfigured",
    }:
        raise CredentialError("invalid")
    revision, credential, configured, verification = (
        value["revision"],
        value["credentialRevision"],
        value["configured"],
        value["verification"],
    )
    if (
        type(revision) is not int
        or revision < 0
        or type(credential) is not int
        or credential < 0
        or type(configured) is not bool
        or not isinstance(verification, str)
        or verification not in {"unchecked", "ok", "failed"}
    ):
        raise CredentialError("invalid")
    try:
        user = parse_slack_user(value["userId"])
    except ValueError:
        raise CredentialError("invalid") from None
    app_revision, app_configured = value["appRevision"], value["appConfigured"]
    if (
        type(app_revision) is not int
        or app_revision < 0
        or type(app_configured) is not bool
        or (app_configured and app_revision == 0)
    ):
        raise CredentialError("invalid")
    workspace, bot = value["workspace"], value["bot"]
    if (
        (workspace is not None and not isinstance(workspace, str))
        or (bot is not None and not isinstance(bot, str))
        or (configured and credential == 0)
        or (not configured and verification != "unchecked")
    ):
        raise CredentialError("invalid")
    return SlackSettingsState(
        revision,
        credential,
        user,
        configured,
        "ok" if verification == "ok" else "failed" if verification == "failed" else "unchecked",
        SlackIdentity(workspace, bot),
        app_revision,
        app_configured,
    )
