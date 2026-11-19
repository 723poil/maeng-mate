"""macOS 메뉴바의 최소 환경과 로컬 클라이언트를 조립한다."""

import os
import signal
import subprocess
import sys
import threading
from collections.abc import Mapping
from contextlib import ExitStack
from functools import partial
from pathlib import Path
from types import FrameType

from src.application.desktop import preview_departure
from src.application.notifications import NotificationRunner
from src.application.runtime_settings_service import RestartOperation
from src.bootstrap.app_settings import application_environment
from src.bootstrap.environment import ConfigError, McpEnvironment, local_api_settings
from src.bootstrap.executable import executable_command, execution_environment
from src.bootstrap.instance import application_directory, project_root
from src.bootstrap.notification_channels import CHANNELS, build_senders
from src.bootstrap.notification_worker import NotificationWorker
from src.domain.daily_work import FlexibleDeparturePolicy
from src.infrastructure.ai_skills import UserSkillInstaller
from src.infrastructure.calendar_api import CalendarApi
from src.infrastructure.desktop_api import DesktopApi
from src.infrastructure.macos_notification_app import MacNotificationApp
from src.infrastructure.mcp_settings_api import McpSettingsApi
from src.infrastructure.notification_api import LocalNotificationQueue
from src.infrastructure.notion_credentials_api import NotionCredentialsApi
from src.infrastructure.preferences_api import PreferencesApi
from src.infrastructure.runtime_settings_api import RuntimeSettingsApi
from src.infrastructure.slack_notification_api import SlackNotificationApi
from src.infrastructure.slack_settings_api import SlackSettingsApi
from src.infrastructure.source_settings_api import SourceSettingsApi
from src.infrastructure.work_records_api import WorkRecordsApi
from src.infrastructure.work_rules_api import WorkRulesApi


def restart_services() -> RestartOperation:
    """관리 명령을 분리 실행하고 메뉴바 종료를 기다리는 교착을 만들지 않는다."""
    return subprocess.Popen(
        executable_command("service", "restart"),
        cwd=project_root(),
        env=execution_environment(os.environ),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )


def run_menubar(env: Mapping[str, str]) -> None:
    """GUI에 DB·Notion 환경을 전달하지 않고 공통 API 값만 읽는다."""
    if sys.platform != "darwin":
        raise ConfigError("메뉴바는 macOS에서만 실행할 수 있습니다.")
    port, internal_token = local_api_settings(application_environment(env))
    environment = McpEnvironment(f"http://127.0.0.1:{port}", internal_token)
    try:
        from src.presentation.menubar import MenuBarApp
    except ImportError:
        raise ConfigError("메뉴바 설치가 필요합니다: uv sync --extra menubar --locked") from None
    preferences = PreferencesApi(
        environment.api_url,
        environment.api_token,
        channels=tuple(channel.key for channel in CHANNELS),
    )
    notification_app = MacNotificationApp(application_directory() / "NotificationAgent.app")
    notification_queue = LocalNotificationQueue(environment.api_url, environment.api_token)
    notifications = NotificationWorker(
        NotificationRunner(
            notification_queue,
            build_senders(
                notification_app,
                preferences,
                notification_queue,
                SlackNotificationApi(environment.api_url, environment.api_token),
            ),
        )
    )
    stop_requested = threading.Event()

    def request_stop(signum: int, frame: FrameType | None) -> None:
        """OS 종료 요청은 메인 루프에 넘겨 마지막 자동 저장을 마무리한다."""
        stop_requested.set()

    with ExitStack() as cleanup:
        for sig in (signal.SIGINT, signal.SIGTERM):
            previous = signal.signal(sig, request_stop)
            cleanup.callback(signal.signal, sig, previous)
        notifications.start()
        # 알림 작업자를 먼저 정리한 뒤 기존 신호 처리기로 복귀한다.
        cleanup.callback(notifications.close)
        MenuBarApp(
            DesktopApi(environment.api_url, environment.api_token),
            partial(preview_departure, policy=FlexibleDeparturePolicy()),
            preferences_client=preferences,
            notification_branding=notification_app,
            work_records_client=WorkRecordsApi(environment.api_url, environment.api_token),
            work_rules_client=WorkRulesApi(environment.api_url, environment.api_token),
            calendar_client=CalendarApi(environment.api_url, environment.api_token),
            slack_settings_client=SlackSettingsApi(environment.api_url, environment.api_token),
            runtime_settings_client=RuntimeSettingsApi(environment.api_url, environment.api_token),
            notion_credentials_client=NotionCredentialsApi(
                environment.api_url, environment.api_token
            ),
            mcp_settings_client=McpSettingsApi(environment.api_url, environment.api_token),
            skills=UserSkillInstaller(Path.home()),
            source_client=SourceSettingsApi(environment.api_url, environment.api_token),
            restart_services=restart_services,
            notification_channels=CHANNELS,
            notification_status=notifications.status,
            shutdown_requested=stop_requested.is_set,
        ).run()
