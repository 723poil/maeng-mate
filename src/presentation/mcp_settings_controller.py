"""MCP 관리 화면의 비동기 조회와 명시적 발급·폐기를 관리한다."""

from concurrent.futures import Future, ThreadPoolExecutor

from src.application.ai_skills import (
    SkillInstaller,
    SkillInstallError,
    SkillInstallResult,
    SkillTarget,
)
from src.application.credentials import CredentialSecret
from src.application.mcp_access import (
    IssuedMcpToken,
    McpAccessError,
    McpSettingsClient,
    McpSettingsState,
)
from src.presentation.read_retry import ReadRetry


class McpSettingsController:
    """읽기만 재시도하고 발급된 원문은 메모리에서 잠시 보유한다."""

    def __init__(self, client: McpSettingsClient, skills: SkillInstaller | None = None) -> None:
        """관리 클라이언트를 주입하고 최초 상태를 자동 조회한다."""
        self._client = client
        self._skills = skills
        self._installing: Future[SkillInstallResult] | None = None
        self.skill_message: str | None = None
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="mcp-settings")
        self._pending: Future[McpSettingsState] | None = None
        self._issuing: Future[IssuedMcpToken] | None = None
        self._closed = False
        self._read_retry = ReadRetry()
        self._writing = False
        self.state: McpSettingsState | None = None
        self.error: str | None = None
        self.blocked = False
        self.pending_enabled: bool | None = None
        self.one_time_token: CredentialSecret | None = None
        self.secret_generation = 0
        self.issue_generation = 0
        self._retain_issue_secret = False
        self._issued_token_id: str | None = None
        self.reload()

    @property
    def busy(self) -> bool:
        """조회·발급·폐기를 동시에 실행하지 않는다."""
        return (
            self._pending is not None or self._issuing is not None or self._installing is not None
        )

    @property
    def can_install_skill(self) -> bool:
        """로컬 설치 경계가 연결되어 있고 요청 중이 아닐 때만 허용한다."""
        return self._skills is not None and not self.busy

    def install_skill(self, target: SkillTarget) -> None:
        """인증·MCP 연결 설정을 변경하지 않고 지침만 설치한다."""
        if self._closed or not self.can_install_skill or self._skills is None:
            return
        self.skill_message = None
        self._installing = self._executor.submit(self._skills.install, target)

    @property
    def editable(self) -> bool:
        """확정 상태를 읽은 뒤에만 관리 작업을 허용한다."""
        return (
            not self._closed
            and self.state is not None
            and not self.busy
            and not self.blocked
            and not self._read_retry.waiting
        )

    def reload(self) -> None:
        """쓰기 결과가 불명확해도 새 원문을 발급하지 않고 목록만 확인한다."""
        if self._closed or self.busy:
            return
        self.error = None
        self.blocked = False
        self._writing = False
        self._read_retry.cancel()
        self._pending = self._executor.submit(self._client.read)

    def set_enabled(self, enabled: bool) -> None:
        """MCP 사용 변경을 저장하고 중복 클릭을 막는다."""
        if self.editable and self.state is not None:
            self.skill_message = None
            self._writing = True
            self.pending_enabled = enabled
            self.error = None
            self._pending = self._executor.submit(
                self._client.set_enabled, self.state.snapshot.revision, enabled
            )

    def issue(self, name: str, days: int | None) -> None:
        """발급 요청을 한 번 제출하며 이전 발급 원문을 폐기한다."""
        if not self.editable:
            return
        self.skill_message = None
        if not 1 <= len(" ".join(name.split())) <= 60:
            self.error = "토큰 이름을 입력하세요."
            return
        self.forget_secret()
        self._retain_issue_secret = True
        self.error = None
        self._writing = True
        self._issuing = self._executor.submit(self._client.issue, name, days)

    def revoke(self, identifier: str) -> None:
        """현재 목록의 ID만 사용해 폐기를 요청한다."""
        if not self.editable:
            return
        self.skill_message = None
        self.error = None
        self._writing = True
        if identifier == self._issued_token_id:
            self.forget_secret()
        self._pending = self._executor.submit(self._client.revoke, identifier)

    def forget_secret(self) -> None:
        """창을 닫거나 다음 발급을 시작하면 원문을 다시 표시하지 않는다."""
        self.one_time_token = None
        self._issued_token_id = None
        self._retain_issue_secret = False
        self.secret_generation += 1

    def tick(self) -> None:
        """늦은 응답은 한 번만 반영하고 미확정 쓰기는 반복하지 않는다."""
        if self._closed:
            return
        if self._installing is not None and self._installing.done():
            installing, self._installing = self._installing, None
            try:
                result = installing.result()
                self.skill_message = "·".join(result.targets) + " 스킬 설치됨"
            except SkillInstallError as error:
                self.skill_message = (
                    "설치 폴더에 다른 내용이 있어 기존 파일을 그대로 두었습니다."
                    if error.code == "modified"
                    else "스킬 설치 폴더와 쓰기 권한을 확인하세요."
                )
            except Exception:
                self.skill_message = "스킬을 설치하지 못했습니다."
        if self._read_retry.due:
            self.reload()
        if self._issuing is not None and self._issuing.done():
            future, self._issuing = self._issuing, None
            try:
                issued = future.result()
                self.state = issued.state
                self.one_time_token = issued.token if self._retain_issue_secret else None
                self._issued_token_id = issued.id if self._retain_issue_secret else None
                self.secret_generation += 1
                self.issue_generation += 1
                self._success()
            except Exception as error:
                self._failed(error)
        if self._pending is not None and self._pending.done():
            pending, self._pending = self._pending, None
            try:
                self.state = pending.result()
                self._success()
            except Exception as error:
                self._failed(error)

    def _success(self) -> None:
        """확정 응답 이후 재시도 간격과 오류를 초기화한다."""
        self.pending_enabled = None
        self._read_retry.reset()
        self.error = None
        self._writing = False

    def _failed(self, error: Exception) -> None:
        """쓰기 실패는 발급 여부를 확인할 때까지 잠그고 조회만 재시도한다."""
        self.pending_enabled = None
        if self._writing:
            self.blocked = True
            self.error = "다시 불러와 MCP 설정과 토큰 목록을 확인하세요."
            if isinstance(error, McpAccessError) and error.code == "invalid":
                self.blocked = False
                self.error = "이름과 기간을 확인하세요."
        else:
            self.error = "MCP 상태를 불러오지 못했습니다."
            self._read_retry.schedule()

    @property
    def message(self) -> str:
        """관리 작업의 진행과 원문 보관 조건을 짧게 안내한다."""
        if self._installing is not None:
            return "스킬 설치 중…"
        if self.busy:
            return "처리 중…" if self._writing else "불러오는 중…"
        if self.skill_message:
            return self.skill_message
        if self.error:
            return self.error
        if self.one_time_token is not None:
            return "창을 닫으면 다시 복사할 수 없습니다. 지금 ‘토큰 복사’를 누르세요."
        return ""

    def close(self) -> None:
        """수명을 마칠 때 발급 원문과 작업자를 정리한다."""
        self._closed = True
        self.forget_secret()
        self._executor.shutdown(wait=False, cancel_futures=True)
