"""Notion 인증 작업을 비동기로 실행하고 쓰기 재전송을 막는다."""

from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import replace
from typing import Literal

from src.application.credentials import CredentialError, CredentialSecret
from src.application.notion_credentials import NotionCredentialsClient, NotionCredentialState
from src.presentation.read_retry import ReadRetry
from src.presentation.registration_status import registration_status


class NotionCredentialsController:
    """입력 비밀값을 응답 상태와 분리하고 성공 후에만 입력을 비운다."""

    def __init__(self, client: NotionCredentialsClient) -> None:
        """보안 저장소 대신 인증 관리 API를 주입한다."""
        self._client = client
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="notion-credentials")
        self._pending: Future[NotionCredentialState] | None = None
        self._read_retry = ReadRetry()
        self._closed = False
        self.state: NotionCredentialState | None = None
        self.operation: Literal["read", "check", "save", "delete"] = "read"
        self.error: str | None = None
        self.error_code: str | None = None
        self.blocked = False
        self.input_dirty = False
        self.clear_generation = 0
        self.reload()

    @property
    def busy(self) -> bool:
        """관리 요청은 한 번에 하나만 실행한다."""
        return self._pending is not None

    @property
    def editable(self) -> bool:
        """조회 실패·불명확한 쓰기 뒤에는 다시 읽기 전까지 잠근다."""
        return (
            not self._closed
            and self.state is not None
            and not self.busy
            and not self.blocked
            and not self._read_retry.waiting
        )

    def reload(self, *, allow_prompt: bool = False) -> None:
        """명시적 다시 읽기에서 입력을 폐기하고 확정 메타데이터만 가져온다."""
        if self._closed or self.busy:
            return
        self.input_dirty = False
        self.clear_generation += 1
        self._read_retry.reset()
        self._read(allow_prompt=allow_prompt)

    def _read(self, *, allow_prompt: bool = False) -> None:
        """기본 토큰 입력 없이 상태만 다시 조회한다."""
        self.operation = "read"
        self.error, self.error_code = None, None
        self.blocked = False
        self._read_retry.cancel()
        self._pending = self._executor.submit(self._client.read, allow_prompt=allow_prompt)

    def edited(self, has_input: bool) -> None:
        """보안 입력 문자열은 보관하지 않고 입력 여부만 기록한다."""
        self.input_dirty = has_input
        if not self.blocked:
            self.error, self.error_code = None, None

    def perform(
        self, operation: Literal["check", "save", "delete"], token: CredentialSecret | None = None
    ) -> None:
        """쓰기·확인 작업을 명시적 사용자 입력에서 한 번만 제출한다."""
        if self._closed or not self.editable or self.state is None:
            return
        self.operation = operation
        self.error, self.error_code = None, None
        revision = self.state.revision
        if operation == "save":
            if token is None:
                self.error_code = "invalid"
                self.error = "토큰 형식을 확인하세요."
                return
            self._pending = self._executor.submit(self._client.save, revision, token)
        elif operation == "delete":
            self._pending = self._executor.submit(self._client.delete, revision)
        else:
            self._pending = self._executor.submit(self._client.check, revision)

    def tick(self) -> None:
        """조회만 재시도하고 저장 결과가 불명확하면 다시 읽기를 요구한다."""
        if self._closed:
            return
        if self._read_retry.due:
            self._read()
        if self._pending is None or not self._pending.done():
            return
        future, self._pending = self._pending, None
        try:
            self.state = future.result()
            self._read_retry.reset()
            if self.operation in {"save", "delete"}:
                self.clear_generation += 1
                self.input_dirty = False
        except CredentialError as error:
            if self.operation == "check" and self.state is not None:
                self.state = replace(self.state, verification="failed")
            self.error_code = error.code
            self.error = {
                "unauthorized": "토큰 인증에 실패했습니다.",
                "permission": "수집 소스 접근 권한을 확인하세요.",
                "keychain": "‘다시 불러오기’를 눌러 키체인 접근을 허용하세요.",
                "conflict": "인증 정보가 바뀌었습니다. 다시 불러오세요.",
                "busy": "다른 인증 작업이 진행 중입니다.",
                "invalid": "토큰 형식을 확인하세요.",
                "missing": "먼저 토큰을 등록하세요.",
                "timeout": "응답이 늦습니다. 다시 확인해 주세요.",
                "rate_limited": "요청이 많습니다. 잠시 후 다시 확인하세요.",
            }.get(error.code, "토큰 상태를 확인하지 못했습니다.")
            if self.operation == "read":
                # 키체인 승인에는 사용자 동작이 필요하므로 재조회 버튼을 기다린다.
                self.blocked = error.code == "keychain"
                if not self.blocked:
                    self._read_retry.schedule()
            else:
                # 확인은 읽기 작업이므로 실패 후 다시 누를 수 있다. 불명확한 쓰기만 잠근다.
                self.blocked = error.code in {"conflict", "keychain"} or (
                    self.operation in {"save", "delete"}
                    and error.code in {"unavailable", "timeout"}
                )
        except Exception:
            self.error_code, self.error = "unavailable", "다시 불러와 토큰 상태를 확인하세요."
            self.blocked = self.operation != "check"

    @property
    def restart_required(self) -> bool:
        """확정 응답에 기록된 적용 대기만 사용한다."""
        return self.state is not None and self.state.restart_required

    @property
    def badge(self) -> tuple[str, str]:
        """색상과 짧은 문구를 함께 사용해 처리 상태를 구분한다."""
        state = self.state
        return registration_status(
            configured=bool(state and state.configured),
            verification=state.verification if state else "unchecked",
            dirty=self.input_dirty,
            busy=self.busy,
            error=bool(self.error),
            error_code=self.error_code,
        )

    def close(self) -> None:
        """추가 작업을 막고 민감한 입력을 가진 실행을 자동 반복하지 않는다."""
        self._closed = True
        self._read_retry.cancel()
        self._executor.shutdown(wait=False, cancel_futures=True)
