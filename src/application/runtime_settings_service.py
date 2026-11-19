"""저장 설정과 실행 중인 설정을 구분하는 편집 계약이다."""

from dataclasses import dataclass
from typing import Protocol

from src.application.runtime_settings import (
    RuntimeSettingsStore,
    parse_runtime_settings,
    runtime_settings_payload,
)
from src.domain.runtime_settings import RuntimeSettings


@dataclass(frozen=True)
class RuntimeSettingsState:
    """편집할 설정과 초기화·재시작 필요 여부다."""

    settings: RuntimeSettings
    initialized: bool
    restart_required: bool


class RuntimeSettingsClient(Protocol):
    """화면에서 DB·환경 대신 호출하는 편집 포트다."""

    def read(self) -> RuntimeSettingsState:
        """저장 상태와 현재 실행에 적용됐는지 조회한다."""
        ...

    def save(self, value: RuntimeSettings) -> RuntimeSettingsState:
        """읽은 버전에 한해서 명시적으로 저장한다."""
        ...


class RuntimeSettingsService:
    """시작 시 실제 조립한 값과 저장 값을 비교해 적용 대기를 알린다."""

    def __init__(self, store: RuntimeSettingsStore, active: RuntimeSettings) -> None:
        """활성값은 저장 요청으로 바꾸지 않고 이번 프로세스 동안 유지한다."""
        self._store = store
        self._active = active

    def read(self) -> RuntimeSettingsState:
        """미초기화 조회는 활성값을 보여주되 기본 행을 만들지 않는다."""
        stored = self._store.read()
        return self._state(stored or self._active, stored is not None)

    def save(self, value: RuntimeSettings) -> RuntimeSettingsState:
        """최초 저장도 버전 조건을 유지하고 실행값을 즉시 바꾸지 않는다."""
        return self._state(self._store.save(value), True)

    def _state(self, value: RuntimeSettings, initialized: bool) -> RuntimeSettingsState:
        """값이 같으면 버전 증가만으로 재시작을 요구하지 않는다."""
        return RuntimeSettingsState(
            value,
            initialized,
            value.general != self._active.general or value.notion != self._active.notion,
        )


def runtime_state_payload(value: RuntimeSettingsState) -> dict[str, object]:
    """편집 API에 필요한 값만 직렬화한다."""
    return {
        "settings": runtime_settings_payload(value.settings),
        "initialized": value.initialized,
        "restartRequired": value.restart_required,
    }


def parse_runtime_state(value: object) -> RuntimeSettingsState:
    """로컬 응답도 키·타입·초기화 버전 관계를 검사한다."""
    if not isinstance(value, dict) or set(value) != {"settings", "initialized", "restartRequired"}:
        raise ValueError("설정 응답을 확인하세요.")
    initialized, restart = value["initialized"], value["restartRequired"]
    if type(initialized) is not bool or type(restart) is not bool:
        raise ValueError("설정 상태를 확인하세요.")
    settings = parse_runtime_settings(value["settings"])
    if initialized != (settings.revision > 0):
        raise ValueError("설정 버전을 확인하세요.")
    return RuntimeSettingsState(settings, initialized, restart)


class RestartOperation(Protocol):
    """관리 서비스 재시작의 비차단 진행 확인 계약이다."""

    def poll(self) -> int | None:
        """실행 중이면 None, 끝나면 종료 코드를 반환한다."""
        ...
