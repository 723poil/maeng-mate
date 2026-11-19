"""Slack 화면의 조회·독립 저장·인증 확인을 비동기로 관리한다."""

from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import replace
from typing import Literal

from src.application.credentials import CredentialError, CredentialSecret
from src.application.runtime_settings import parse_slack_user
from src.application.slack_settings import (
    SlackIdentity,
    SlackSettingsClient,
    SlackSettingsState,
    SlackTestError,
)
from src.presentation.read_retry import ReadRetry

Operation = Literal["read", "member", "token", "check", "delete", "app-token", "app-delete", "test"]
MESSAGES = {
    "busy": "다른 테스트 알림을 보내고 있습니다.",
    "recipient": "멤버 ID와 Slack 앱의 메시지 탭 설정을 확인하세요.",
    "unverified": "먼저 봇 토큰 아래의 ‘연결 확인’을 누르세요.",
    "delivery_unknown": "발송 여부를 확인하지 못했습니다. Slack DM을 먼저 확인하세요.",
    "invalid": "멤버 ID 또는 봇 토큰 형식을 확인하세요.",
    "conflict": "설정이 변경됐습니다. 다시 불러와 주세요.",
    "keychain": "Keychain 접근을 확인하세요.",
    "unauthorized": "토큰이 유효하지 않거나 만료됐습니다.",
    "permission": "봇 토큰과 앱 권한을 확인하세요.",
    "timeout": "Slack 응답 시간이 초과됐습니다.",
    "rate_limited": "Slack 요청이 많습니다. 잠시 후 다시 확인하세요.",
    "missing": "먼저 봇 토큰을 저장하세요.",
    "unavailable": "Slack 설정을 다시 불러와 확인하세요.",
}


class SlackSettingsController:
    """초기 읽기는 재시도하고 모호한 쓰기는 재조회 전 잠근다."""

    def __init__(self, client: SlackSettingsClient, on_member_saved: Callable[[], None]) -> None:
        """UI가 저장소나 외부 API를 만들지 않게 상태 포트만 받는다."""
        self._client, self._on_member_saved = client, on_member_saved
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="slack-settings")
        self._pending: Future[SlackSettingsState] | None = None
        self._operation: Operation = "read"
        self._closed = False
        self._read_retry = ReadRetry()
        self.state: SlackSettingsState | None = None
        self.member_dirty = False
        self.token_dirty = False
        self.app_dirty = False
        self.app_clear_generation = 0
        self.blocked = False
        self.notice = ""
        self.error: str | None = None
        self.error_code: str | None = None
        self.member_generation = 0
        self.clear_generation = 0
        self.reload()

    @property
    def operation(self) -> Operation:
        """화면이 현재 작업에 해당하는 카드의 상태만 표시하게 한다."""
        return self._operation

    def invalid_token(self, *, app: bool = False) -> None:
        """검증에 실패한 입력의 카드에만 오류를 표시한다."""
        self._operation = "app-token" if app else "token"
        self.error = (
            "xapp-로 시작하는 앱 토큰을 입력하세요." if app else "봇 토큰 형식을 확인하세요."
        )
        self.error_code = "invalid"

    @property
    def busy(self) -> bool:
        """응답을 반영할 때까지 중복 요청을 막는다."""
        return self._pending is not None

    @property
    def editable(self) -> bool:
        """확정 상태·연결을 읽은 뒤에만 저장을 허용한다."""
        return (
            not self._closed
            and self.state is not None
            and not self.busy
            and not self.blocked
            and not self._read_retry.waiting
        )

    def reload(self) -> None:
        """명시적 재조회에서만 초안과 불명확한 쓰기 잠금을 초기화한다."""
        if self._closed or self.busy:
            return
        self.member_dirty = False
        self.token_dirty = False
        self.app_dirty = False
        self.app_clear_generation += 1
        self.clear_generation += 1
        self.notice = ""
        self.error = None
        self.error_code = None
        self.blocked = False
        self._read_retry.cancel()
        self._operation = "read"
        self._pending = self._executor.submit(self._client.read)

    def edited(self, *, token: bool) -> None:
        """탭 이동에서도 자동 저장 없이 입력 초안을 유지한다."""
        if not self.editable:
            return
        if token:
            self.token_dirty = True
        else:
            self.member_dirty = True
        self.notice = ""
        self.error = None
        self.error_code = None

    def save_member(self, value: str) -> None:
        """수신자 변경만 검증해 저장하며 토큰 입력은 건드리지 않는다."""
        if not self.editable or self.state is None:
            return
        self._operation = "member"
        try:
            user = parse_slack_user(value)
        except ValueError:
            self.error = MESSAGES["invalid"]
            self.error_code = "invalid"
            return
        self._operation = "member"
        self.notice = ""
        self.error = None
        self.error_code = None
        self._pending = self._executor.submit(self._client.save_member, self.state.revision, user)

    def credential(
        self,
        action: Literal["token", "check", "delete", "app-token", "app-delete"],
        token: CredentialSecret | None = None,
    ) -> None:
        """읽은 Keychain 버전에 한해 저장·확인·삭제를 한 번 실행한다."""
        if not self.editable or self.state is None:
            return
        self._operation = action
        self.notice = ""
        self.error = None
        self.error_code = None
        if action == "token" and token is not None:
            self._pending = self._executor.submit(
                self._client.save_token, self.state.credential_revision, token
            )
        elif action == "check":
            self._pending = self._executor.submit(
                self._client.check, self.state.credential_revision
            )
        elif action == "delete":
            self._pending = self._executor.submit(
                self._client.delete_token, self.state.credential_revision
            )

    @property
    def can_test(self) -> bool:
        """미저장 수신자·토큰 대신 인증된 저장 값으로만 테스트한다."""
        state = self.state
        return bool(
            self.editable
            and state
            and state.user_id
            and state.verification == "ok"
            and not self.member_dirty
            and not self.token_dirty
        )

    def test_notification(self) -> None:
        """한 번 누른 테스트만 비동기로 실행하며 자동 재시도하지 않는다."""
        if not self.can_test or self.state is None:
            return
        self._operation = "test"
        self.notice = ""
        self.error = None
        self.error_code = None
        self._pending = self._executor.submit(
            self._client.test_notification, self.state.revision, self.state.credential_revision
        )

    def app_credential(self, token: CredentialSecret | None = None) -> None:
        """앱 토큰 저장과 삭제는 봇 인증·외부 연결 없이 별도 버전으로 실행한다."""
        if not self.editable or self.state is None:
            return
        self.notice = ""
        self.error = None
        self.error_code = None
        self._operation = "app-token" if token else "app-delete"
        self._pending = (
            self._executor.submit(self._client.save_app_token, self.state.app_revision, token)
            if token
            else self._executor.submit(self._client.delete_app_token, self.state.app_revision)
        )

    def tick(self) -> None:
        """완료된 상태만 반영하고 다른 카드의 미저장 입력을 보존한다."""
        if self._closed:
            return
        if self._read_retry.due:
            self.reload()
        if self._pending is None or not self._pending.done():
            return
        pending, self._pending = self._pending, None
        operation = self._operation
        try:
            self.state = pending.result()
            self._read_retry.reset()
            self.notice = ""
            self.error = None
            self.error_code = None
            self.blocked = False
            if operation == "member":
                self.member_dirty = False
            if operation in {"token", "delete"}:
                self.token_dirty = False
                self.clear_generation += 1
            if operation in {"app-token", "app-delete"}:
                self.app_dirty = False
                self.app_clear_generation += 1
            if not self.member_dirty:
                self.member_generation += 1
            if operation == "member":
                self._on_member_saved()
            if operation == "test":
                self.notice = "테스트 알림을 보냈습니다."
        except Exception as error:
            code = (
                error.code
                if isinstance(error, (CredentialError, SlackTestError))
                else "delivery_unknown"
                if operation == "test"
                else "unavailable"
            )
            self.error_code = code
            self.error = MESSAGES.get(code, MESSAGES["unavailable"])
            if operation == "test" and code == "permission":
                self.error = "봇의 chat:write 권한을 확인하고 Slack 앱을 다시 설치하세요."
            # 다른 카드 편집으로 오류 문구가 지워져도 이전 인증 성공을 다시 표시하지 않는다.
            if (
                operation == "check" or (operation == "test" and code == "unauthorized")
            ) and self.state is not None:
                self.state = replace(self.state, verification="failed", identity=SlackIdentity())
            if operation == "read":
                if code == "unavailable":
                    self.error = "잠시 후 Slack 설정을 다시 불러옵니다."
                self._read_retry.schedule()
            elif operation != "check":
                self.blocked = code not in {
                    "invalid",
                    "unauthorized",
                    "permission",
                    "timeout",
                    "rate_limited",
                    "missing",
                }

    def forget_token(self) -> None:
        """창을 닫으면 아직 저장하지 않은 토큰 입력만 지운다."""
        self.token_dirty = False
        self.app_dirty = False
        self.app_clear_generation += 1
        self.clear_generation += 1

    @property
    def message(self) -> str:
        """진행·실패만 푸터에 표시해 카드 배지와 중복하지 않는다."""
        return self.error or (
            "테스트 알림 보내는 중…"
            if self.busy and self._operation == "test"
            else "불러오는 중…"
            if self.busy and self._operation == "read"
            else "연결 확인 중…"
            if self.busy and self._operation in {"check", "token"}
            else "저장 중…"
            if self.busy
            else self.notice
        )

    def close(self) -> None:
        """앱 종료 시 추가 작업을 중단하고 비밀 입력 상태를 지운다."""
        self._closed = True
        self.forget_token()
        self._executor.shutdown(wait=False, cancel_futures=True)
