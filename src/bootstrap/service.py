"""개인 PC의 로그인 자동 실행 설치와 수명을 관리한다."""

import asyncio
import importlib.util
import json
import os
import sys
import time
from collections.abc import Mapping
from pathlib import Path

import httpx

from src.bootstrap.app_settings import application_environment
from src.bootstrap.build_profile import current_profile
from src.bootstrap.deployment import move_active_data, prepare_runtime
from src.bootstrap.environment import (
    ConfigError,
    app_environment,
    local_api_settings,
    required,
    sync_interval,
)
from src.bootstrap.executable import executable_command, packaged
from src.bootstrap.instance import (
    Role,
    application_directory,
    coordination_lock,
    instance_lock,
    owned_pid,
    project_root,
    stop_owned_process,
)
from src.bootstrap.runtime_settings import load_runtime_environment
from src.bootstrap.storage import require_migrated_database
from src.bootstrap.work_settings import load_work_settings
from src.infrastructure.local_http import local_client
from src.infrastructure.macos_launchd import AgentDefinition, LaunchdError, MacLaunchd
from src.infrastructure.sqlite.connection import Database

LABELS: dict[Role, str] = {
    "resident": f"{current_profile().identifier}.resident",
    "menubar": f"{current_profile().identifier}.menubar",
}


def definitions(root: Path) -> tuple[AgentDefinition, ...]:
    """실행 경로는 현재 프로젝트에 고정하고 비밀값은 등록 파일에서 제외한다."""
    working_directory = application_directory() if packaged() else root
    logs = working_directory / ("logs" if packaged() else ".cache")
    return tuple(
        AgentDefinition(
            label,
            executable_command("agent", role, source_root=root),
            working_directory,
            logs / (role + ".log"),
            "Interactive" if role == "menubar" else "Background",
        )
        for role, label in LABELS.items()
    )


def preflight(*, create_admin: bool = False) -> dict[str, str]:
    """실행을 중지하기 전에 설정·의존성과 기존 DB 준비를 확인한다."""
    if packaged() and create_admin:
        from src.bootstrap.packaged_app import prepare_connection

        values = prepare_connection()
        app_environment(values)
        if importlib.util.find_spec("AppKit") is None:
            raise ConfigError("앱의 실행 파일을 확인하세요. 앱을 다시 설치하세요.")
        return values
    values = application_environment({}, create_admin=create_admin)
    if create_admin and not Path(required(values, "DATABASE_PATH")).exists():
        # 분리 후 첫 개발 설치는 자기 저장소에만 스키마를 준비한다.
        from src.bootstrap.packaged_app import initialize_database

        with instance_lock("resident"), coordination_lock("storage"):
            initialize_database(required(values, "DATABASE_PATH"))
    app_environment(load_runtime_environment(values))
    sync_interval(values)
    database = Database(required(values, "DATABASE_PATH"))
    asyncio.run(require_migrated_database(database))
    if create_admin:
        from src.bootstrap.work_settings import initialize_work_settings

        initialize_work_settings(database)
    load_work_settings(database)
    if (
        not packaged() and not os.access(project_root() / ".venv/bin/python", os.X_OK)
    ) or importlib.util.find_spec("AppKit") is None:
        raise ConfigError("실행 환경을 먼저 설치하세요: uv sync --locked --extra menubar")
    return values


def api_ready(values: dict[str, str]) -> bool:
    """인증된 준비 상태만 확인하고 토큰·개인 자료를 출력하지 않는다."""
    try:
        port, token = local_api_settings(values)
        with local_client(f"http://127.0.0.1:{port}", token, timeout=1) as client:
            response = client.get("/v1/status")
        return response.status_code == 200
    except (httpx.HTTPError, ConfigError):
        return False


def stop_apps(manager: MacLaunchd, root: Path) -> None:
    """재시작을 먼저 해제한 뒤 GUI와 수집 서버를 순서대로 종료한다."""
    for role in ("menubar", "resident"):
        manager.stop(LABELS[role])
        stop_owned_process(root, role)


def start_apps(manager: MacLaunchd, root: Path) -> dict[Role, int]:
    """미등록인 수동 실행은 인계하고 등록된 작업은 중복 생성하지 않는다."""
    started: dict[Role, int] = {}
    for role, label in LABELS.items():
        if not manager.path(label).is_file():
            raise ConfigError("자동 실행 등록이 없습니다. service install을 먼저 실행하세요.")
        if not manager.registered(label):
            stop_owned_process(root, role)
        previous = owned_pid(root, role)
        pid = manager.start(label)
        if previous is not None and previous != pid:
            # 등록돼 있지만 중지된 작업 뒤에 수동 실행이 생긴 경우도 명시적으로 인계한다.
            stop_owned_process(root, role, expected_pid=previous)
            pid = manager.start(label)
        started[role] = pid
    return started


def _status_report(
    manager: MacLaunchd, pids: Mapping[Role, int | None], *, ready: bool
) -> dict[str, object]:
    """확인한 PID와 API 응답을 개인 경로 없는 상태로 표시한다."""
    return {
        "installed": all(manager.path(label).is_file() for label in LABELS.values()),
        "resident": {
            "registered": manager.registered(LABELS["resident"]),
            "running": pids["resident"] is not None,
            "apiReady": ready,
        },
        "menubar": {
            "registered": manager.registered(LABELS["menubar"]),
            "running": pids["menubar"] is not None,
        },
    }


def service_status(manager: MacLaunchd, root: Path, values: dict[str, str]) -> dict[str, object]:
    """명시적 상태 조회는 프로세스와 인증된 API를 매번 새로 확인한다."""
    pids = {role: owned_pid(root, role) for role in LABELS}
    return _status_report(manager, pids, ready=api_ready(values))


def wait_ready(
    manager: MacLaunchd, root: Path, values: dict[str, str], expected: dict[Role, int]
) -> dict[str, object]:
    """준비 확인을 통과한 API 응답과 PID로 시작 결과를 반환한다."""
    # 로그인 직후나 설치본 교체 직후의 초기화 지연을 허용하고, 준비되면 즉시 반환한다.
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        if api_ready(values) and all(
            owned_pid(root, role) == pid for role, pid in expected.items()
        ):
            # 두 PID를 방금 확인했으므로 성공 결과를 만들기 위해 다시 조회하지 않는다.
            return _status_report(manager, expected, ready=True)
        time.sleep(0.25)
    raise ConfigError(
        "자동 실행 등록 후 앱 준비를 확인하지 못했습니다. service status와 로컬 로그를 확인하세요."
    )


def _run_service(action: str) -> None:
    """사용자 로그인 도메인의 앱 두 개만 설치·시작·중지·조회한다."""
    if sys.platform != "darwin":
        raise ConfigError("자동 실행은 macOS에서만 지원합니다.")
    if action not in {"install", "start", "stop", "restart", "status", "uninstall"}:
        raise ConfigError(
            "service 동작은 install/start/stop/restart/status/uninstall 중 하나입니다."
        )
    root = project_root()
    os.chdir(root)
    manager = MacLaunchd(os.getuid(), Path.home() / "Library/LaunchAgents")
    values: dict[str, str] = {}
    expected: dict[Role, int] = {}
    try:
        if action in {"install", "start", "restart"}:
            from src.bootstrap.backup_restore import recover_before_start

            recover_before_start(manager, root)
            values = preflight(create_admin=action == "install")
        elif action == "status":
            try:
                values = application_environment({})
            except ConfigError:
                pass  # 설정 파일을 잃어도 등록·실행 상태는 확인할 수 있어야 한다.
        if action == "install":
            stop_apps(manager, root)
            # 수동 실행도 같은 잠금을 사용하므로 DB 전환 중 새 기록이 갈라지지 않는다.
            with instance_lock("resident"), instance_lock("menubar"), coordination_lock("storage"):
                if packaged():
                    from src.bootstrap.packaged_app import install_resources, prepare_state

                    prepare_state(values)
                    install_resources()
                    installed = root
                else:
                    installed = prepare_runtime(root)
                    move_active_data(root, installed, values)
            values = application_environment({})
            for definition in definitions(installed):
                manager.write(definition)
            expected = start_apps(manager, root)
        elif action == "start":
            expected = start_apps(manager, root)
        elif action in {"stop", "restart"}:
            stop_apps(manager, root)
            if action == "restart":
                expected = start_apps(manager, root)
        elif action == "uninstall":
            stop_apps(manager, root)
            for label in LABELS.values():
                manager.remove(label)
        if action in {"install", "start", "restart"}:
            status = wait_ready(manager, root, values, expected)
            if packaged() and action == "install":
                from src.bootstrap.packaged_app import record_installation

                record_installation()
        else:
            status = service_status(manager, root, values)
        print(json.dumps(status, ensure_ascii=False, indent=2))
    except LaunchdError as error:
        raise ConfigError(str(error)) from None


def run_service(action: str) -> None:
    """설치·시작·중지를 직렬화하되 상태 확인은 언제든 허용한다."""
    if action == "finder":
        from src.bootstrap.instance import request_panel
        from src.bootstrap.packaged_app import launch_action

        # 앞선 설치가 완료된 뒤 기록을 읽어 같은 빌드를 다시 설치하지 않는다.
        with coordination_lock("service", wait=True):
            _run_service(launch_action())
            request_panel()
    elif action == "status":
        _run_service(action)
    else:
        with coordination_lock("service"):
            _run_service(action)
