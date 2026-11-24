"""명시적 설정 저장과 조회 재시도·적용 대기를 관리한다."""

from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Protocol

from src.application.errors import SettingsInputRejected
from src.application.runtime_settings_service import (
    RestartOperation,
    RuntimeSettingsState,
)
from src.domain.runtime_settings import RuntimeSettings
from src.presentation.read_retry import ReadRetry


class EditableSettingsState(Protocol):
    """폼이 표시할 저장 여부와 적용 대기 상태다."""

    @property
    def initialized(self) -> bool:
        """저장된 설정의 유무다."""
        ...

    @property
    def restart_required(self) -> bool:
        """재시작으로 적용할 변경이 있는지 나타낸다."""
        ...


class SettingsClient[Value, State: EditableSettingsState](Protocol):
    """명시적 설정 폼이 공통으로 사용하는 조회와 저장이다."""

    def read(self) -> State:
        """현재 폼 값을 읽는다."""
        ...

    def save(self, value: Value) -> State:
        """명시적으로 제출한 값을 한 번 저장한다."""
        ...


class SettingsEditor[Value, State: EditableSettingsState]:
    """읽기만 재시도하고 불명확한 저장은 초안을 보존한다."""

    def __init__(
        self,
        client: SettingsClient[Value, State],
        restart: Callable[[], RestartOperation] | None = None,
    ) -> None:
        """설정 클라이언트와 관리 서비스 재시작만 주입받는다."""
        self._client = client
        self._restart = restart
        self._restart_operation: RestartOperation | None = None
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="runtime-settings")
        self._pending: Future[State] | None = None
        self._saving = False
        self._closed = False
        self._read_retry = ReadRetry()
        self.state: State | None = None
        self.generation = 0
        self.dirty = False
        self.blocked = False
        self.error: str | None = None
        self.reload()

    @property
    def busy(self) -> bool:
        """읽기·저장 중에는 폼 입력을 잠근다."""
        return self._pending is not None

    @property
    def restarting(self) -> bool:
        """종료를 기다리는 작업 스레드 없이 재시작 진행을 확인한다."""
        return self._restart_operation is not None

    @property
    def editable(self) -> bool:
        """조회 재시도·저장 충돌·재시작 중에는 편집하지 않는다."""
        return (
            not self._closed
            and self.state is not None
            and not self.busy
            and not self._read_retry.waiting
            and not self.blocked
            and not self.restarting
        )

    def reload(self) -> None:
        """명시적 다시 읽기에서만 미확정 초안을 버린다."""
        if self._closed or self.busy or self.restarting:
            return
        self._read_retry.reset()
        self._read()

    def _read(self) -> None:
        """백그라운드 조회를 하나만 시작한다."""
        self._read_retry.cancel()
        self.error = None
        self.dirty = False
        self.blocked = False
        self._saving = False
        self._pending = self._executor.submit(self._client.read)

    def edited(self) -> None:
        """문자 입력은 저장하지 않고 폼의 미저장 상태만 표시한다."""
        if self.editable:
            self.dirty = True
            self.error = None

    def save(self, value: Value) -> None:
        """사용자가 저장한 버전만 한 번 전송한다."""
        if self._closed or not self.editable or not self.dirty:
            return
        self.error = None
        self._saving = True
        self._pending = self._executor.submit(self._client.save, value)

    def restart(self, *, provider_pending: bool = False) -> None:
        """저장된 적용 대기 설정만 관리 서비스 재시작으로 반영한다."""
        if (
            self._closed
            or not self.editable
            or self.dirty
            or self.state is None
            or not (self.state.restart_required or provider_pending)
            or self._restart is None
        ):
            return
        try:
            self._restart_operation = self._restart()
        except Exception:
            self.error = "재시작에 실패했습니다. 앱 상태를 확인하세요."

    def tick(self) -> None:
        """완료된 응답만 반영하고 실패한 쓰기는 자동 반복하지 않는다."""
        if self._closed:
            return
        if self._read_retry.due:
            self._read()
        if self._pending is not None and self._pending.done():
            future, self._pending = self._pending, None
            try:
                self.state = future.result()
                self.generation += 1
                self.dirty = False
                self._read_retry.reset()
                self.error = None
            except SettingsInputRejected as error:
                self.error = str(error)
            except Exception:
                if self._saving:
                    self.blocked = True
                    self.error = "다시 불러와 설정이 저장됐는지 확인하세요."
                else:
                    self.error = "잠시 후 설정을 다시 불러옵니다."
                    self._read_retry.schedule()
        if self._restart_operation is not None:
            code = self._restart_operation.poll()
            if code is not None:
                self._restart_operation = None
                if code:
                    self.error = "재시작에 실패했습니다. 앱 상태를 확인하세요."
                else:
                    self.reload()

    @property
    def can_restart(self) -> bool:
        """재시작 어댑터가 있고 저장이 끝난 적용 대기 상태만 허용한다."""
        return (
            self._restart is not None
            and self.editable
            and not self.dirty
            and self.state is not None
            and self.state.restart_required
        )

    @property
    def message(self) -> str:
        """저장 완료와 이번 실행에 적용됐는지를 구분해 안내한다."""
        if self.restarting:
            return "다시 시작 중…"
        if self.busy:
            return "저장 중…" if self._saving else "불러오는 중…"
        if self.error:
            return self.error
        if self.dirty:
            return "수정 중"
        if self.state is not None and self.state.restart_required:
            return "저장됨 · 다시 시작 후 적용"
        # 한 번도 저장하지 않아 기본값을 쓰는 상태는 사용자가 할 일이 없으므로 따로 알리지 않는다.
        if self.state is not None and not self.state.initialized:
            return ""
        return "저장됨"

    def close(self) -> None:
        """재시작 자식 프로세스를 기다리지 않아 서비스 종료 교착을 막는다."""
        self._closed = True
        self._read_retry.cancel()
        self._executor.shutdown(wait=False, cancel_futures=True)


class RuntimeSettingsController(SettingsEditor[RuntimeSettings, RuntimeSettingsState]):
    """일반·Notion 설정 화면의 편집 제어기다."""
