"""launchd가 시작한 실행기에 필요한 환경만 주입한다."""

import os
import sys
from collections.abc import Mapping

from src.bootstrap.app_settings import application_environment
from src.bootstrap.environment import ConfigError, app_environment, local_api_settings
from src.bootstrap.executable import executable_command, execution_environment
from src.bootstrap.instance import Role, project_root
from src.bootstrap.runtime_settings import load_runtime_environment

APP_ENV_KEYS = (
    "LOCAL_API_PORT",
    "LOCAL_API_TOKEN",
    "DATABASE_PATH",
    "NOTION_TOKEN",
    "NOTION_USER_ID",
    "WORK_RELEVANCE_POLICY",
    "SNAPSHOT_MAX_AGE_SECONDS",
    "SYNC_INTERVAL_SECONDS",
)


def child_environment(
    role: Role, values: Mapping[str, str], inherited: Mapping[str, str]
) -> dict[str, str]:
    """메뉴바에는 공통 API 값만, 서버에는 실제 사용하는 앱 설정만 전달한다."""
    result = execution_environment(inherited)
    result["PYTHONUNBUFFERED"] = "1"
    if role == "menubar":
        port, token = local_api_settings(values)
        result.update({"LOCAL_API_PORT": str(port), "LOCAL_API_TOKEN": token})
    else:
        effective = load_runtime_environment(values)
        app_environment(effective)
        result.update({key: effective[key] for key in APP_ENV_KEYS if key in effective})
    return result


def run_agent(role: Role) -> None:
    """등록 파일이나 명령행에 토큰을 남기지 않고 실제 앱으로 교체한다."""
    if sys.platform != "darwin":
        raise ConfigError("자동 실행은 macOS에서만 지원합니다.")
    root = project_root()
    values = application_environment({})
    environment = child_environment(role, values, os.environ)
    command = "run" if role == "resident" else "menubar"
    os.chdir(root)
    arguments = executable_command(command, source_root=root)
    os.execve(arguments[0], arguments, environment)
