"""서비스 관리와 복원 후 시험 기동에 공통인 준비·시작·상태 확인을 수행한다."""

import asyncio
import importlib.util
import os
import time
from collections.abc import Mapping
from pathlib import Path

import httpx

from src.bootstrap.app_settings import application_environment
from src.bootstrap.build_profile import current_profile
from src.bootstrap.environment import (
    ConfigError,
    app_environment,
    local_api_settings,
    required,
    sync_interval,
)
from src.bootstrap.executable import packaged
from src.bootstrap.instance import (
    Role,
    coordination_lock,
    instance_lock,
    owned_pid,
    project_root,
    stop_owned_process,
)
from src.bootstrap.packaged_app import initialize_database, prepare_connection
from src.bootstrap.runtime_settings import load_runtime_environment
from src.bootstrap.storage import require_migrated_database
from src.bootstrap.work_settings import initialize_work_settings, load_work_settings
from src.infrastructure.local_http import local_client
from src.infrastructure.macos_launchd import MacLaunchd
from src.infrastructure.sqlite.connection import Database

LABELS: dict[Role, str] = {
    "resident": f"{current_profile().identifier}.resident",
    "menubar": f"{current_profile().identifier}.menubar",
}


def preflight(*, create_admin: bool = False) -> dict[str, str]:
    """실행을 중지하기 전에 설정·의존성과 기존 DB 준비를 확인한다."""
    if packaged() and create_admin:
        values = prepare_connection()
        app_environment(values)
        if importlib.util.find_spec("AppKit") is None:
            raise ConfigError("앱의 실행 파일을 확인하세요. 앱을 다시 설치하세요.")
        return values
    values = application_environment({}, create_admin=create_admin)
    if create_admin and not Path(required(values, "DATABASE_PATH")).exists():
        # 분리 후 첫 개발 설치는 자기 저장소에만 스키마를 준비한다.
        with instance_lock("resident"), coordination_lock("storage"):
            initialize_database(required(values, "DATABASE_PATH"))
    app_environment(load_runtime_environment(values))
    sync_interval(values)
    database = Database(required(values, "DATABASE_PATH"))
    asyncio.run(require_migrated_database(database))
    if create_admin:
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
