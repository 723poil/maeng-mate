"""개인 PC의 로그인 자동 실행 설치와 수명을 관리한다."""

import json
import os
import sys
from pathlib import Path

from src.bootstrap.app_settings import application_environment
from src.bootstrap.backup_restore import recover_before_start
from src.bootstrap.deployment import move_active_data, prepare_runtime
from src.bootstrap.environment import (
    ConfigError,
)
from src.bootstrap.executable import executable_command, packaged
from src.bootstrap.instance import (
    Role,
    application_directory,
    coordination_lock,
    instance_lock,
    project_root,
    request_panel,
    stop_owned_process,
)
from src.bootstrap.packaged_app import (
    install_resources,
    launch_action,
    prepare_state,
    record_installation,
)
from src.bootstrap.service_startup import LABELS, preflight, service_status, start_apps, wait_ready
from src.infrastructure.macos_launchd import AgentDefinition, LaunchdError, MacLaunchd


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


def stop_apps(manager: MacLaunchd, root: Path) -> None:
    """재시작을 먼저 해제한 뒤 GUI와 수집 서버를 순서대로 종료한다."""
    for role in ("menubar", "resident"):
        manager.stop(LABELS[role])
        stop_owned_process(root, role)


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
                record_installation()
        else:
            status = service_status(manager, root, values)
        print(json.dumps(status, ensure_ascii=False, indent=2))
    except LaunchdError as error:
        raise ConfigError(str(error)) from None


def run_service(action: str) -> None:
    """설치·시작·중지를 직렬화하되 상태 확인은 언제든 허용한다."""
    if action == "finder":
        # 앞선 설치가 완료된 뒤 기록을 읽어 같은 빌드를 다시 설치하지 않는다.
        with coordination_lock("service", wait=True):
            _run_service(launch_action())
            request_panel()
    elif action == "status":
        _run_service(action)
    else:
        with coordination_lock("service"):
            _run_service(action)
