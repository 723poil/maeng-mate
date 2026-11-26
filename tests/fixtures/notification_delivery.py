"""실제 알림 큐·루프백 API·개발 메뉴바를 일회용 자료로 실행한다."""

import asyncio
import json
import os
import secrets
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

import httpx
import uvicorn

from src.application.credentials import CredentialRecord, CredentialSecret
from src.application.notifications import DeliveryResult, NotificationJob
from src.bootstrap.build_profile import BuildProfile
from src.bootstrap.notification_worker import NotificationWorker
from src.domain.preferences import Preferences
from src.infrastructure.notification_api import LocalNotificationQueue
from src.infrastructure.sqlite.connection import Database
from src.infrastructure.sqlite.notifications import SQLiteNotifications
from src.infrastructure.sqlite.preferences import SQLitePreferences
from src.infrastructure.sqlite.schema import migrate
from src.presentation.http import create_app

CHANNELS = ("macos", "slack")


class RecordingSender:
    """외부 발송 대신 호출 시각만 기록하고 실제 ACK 저장은 유지한다."""

    def __init__(self, result: DeliveryResult = "requested") -> None:
        """시나리오별 채널 응답과 완료 관찰 이벤트를 준비한다."""
        self.result = result
        self.calls: list[tuple[str, str, float]] = []
        self.called = threading.Event()

    def request(self, job: NotificationJob) -> DeliveryResult:
        """원문·토큰을 출력하지 않고 전달 식별자와 시각만 보존한다."""
        self.calls.append((job.id, job.channel, time.monotonic()))
        self.called.set()
        return self.result


class DeliveryEnvironment:
    """독립 DB와 인증된 임시 포트만 공유하는 알림 실행 환경이다."""

    def __init__(self, root: Path, port: int, *, token: str | None = None) -> None:
        """운영 설정·Keychain을 읽지 않고 가상 구독을 저장한다."""
        self.root = root
        self.port = port
        self.token = token or secrets.token_urlsafe(48)
        self.database = Database(str(root / "notifications.sqlite3"))
        asyncio.run(migrate(self.database))
        self.preferences = SQLitePreferences(self.database, CHANNELS)
        if token is None:
            self.preferences.save(
                Preferences(
                    notifications_enabled=True,
                    channels=CHANNELS,
                    notification_types=("calendar",),
                )
            )
        self.store = SQLiteNotifications(self.database, channels=CHANNELS)
        self.queue = LocalNotificationQueue(f"http://127.0.0.1:{port}", self.token)
        self.sender = RecordingSender()

    def seed(self, count: int = 6, *, kind: str = "calendar", prefix: str = "job") -> None:
        """동시에 도래한 가상 논리 예약을 저장하며 실제 채널 배정은 claim에 맡긴다."""
        now = datetime.now(UTC).timestamp()
        with self.database.transaction() as connection:
            for index in range(count):
                identifier = f"{prefix}-{index:03d}"
                connection.execute(
                    "INSERT INTO notification_jobs(id,logical_key,kind,title,body,due_at,expires_at,state) VALUES(?,?,?,?,?,?,?,'pending')",
                    (identifier, identifier, kind, "가상 알림", "검증 자료", now, now + 3600),
                )

    def states(self) -> list[dict[str, object]]:
        """채널별 저장 결과만 읽어 외부 요청 횟수와 대조한다."""
        return asyncio.run(
            self.database.fetch(
                "SELECT channel,state,COUNT(*) AS count FROM notification_deliveries GROUP BY channel,state ORDER BY channel,state"
            )
        )

    def mute(self) -> None:
        """발송 승인 직전 설정 변경을 실제 SQLite 저장 경로로 반영한다."""
        self.preferences.save(replace(self.preferences.read(), notifications_enabled=False))


@contextmanager
def delivery_environment() -> Iterator[DeliveryEnvironment]:
    """실제 HTTP 서버를 운영 포트와 겹치지 않게 열고 종료·임시 파일 정리를 보장한다."""
    with tempfile.TemporaryDirectory(prefix="maeng-notification-pdd-") as directory:
        root = Path(directory)
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            environment = DeliveryEnvironment(root, listener.getsockname()[1])
            app = create_app(
                environment.token,
                lambda: {},
                notification_store=environment.store,
                preferences_store=environment.preferences,
                notification_channels=CHANNELS,
            )
            server = uvicorn.Server(uvicorn.Config(app, log_level="error", access_log=False))
            thread = threading.Thread(
                target=server.run, kwargs={"sockets": [listener]}, daemon=True
            )
            thread.start()
            deadline = time.monotonic() + 10
            while not server.started:
                if not thread.is_alive() or time.monotonic() >= deadline:
                    raise RuntimeError("가상 API 시작을 확인하지 못했습니다.")
                time.sleep(0.01)
            try:
                yield environment
            finally:
                server.should_exit = True
                thread.join(timeout=5)
                assert not thread.is_alive()


def run_app(environment: DeliveryEnvironment) -> None:
    """실제 AppKit 메뉴바와 작업자를 고유 개발 프로필로 기동한다."""
    from src.bootstrap.desktop import run_menubar

    profile = BuildProfile(
        "local.maeng-mate.dev.notification-pdd." + secrets.token_hex(6),
        "UnusedNotificationPdd",
        environment.port,
    )

    def monitor() -> None:
        """실제 DB 확정을 기다린 뒤 앱의 SIGTERM 종료 경로를 사용한다."""
        deadline = time.monotonic() + 70
        while time.monotonic() < deadline:
            if (
                sum(
                    row["count"]
                    for row in environment.states()
                    if row["state"] == "requested" and isinstance(row["count"], int)
                )
                == 12
            ):
                break
            time.sleep(0.05)
        # AppKit의 terminate는 프로세스를 끝내므로 결과는 종료 신호 전에 검증·출력한다.
        calls = environment.sender.calls
        if not (
            len(calls) == 12
            and len({item[0] for item in calls}) == 12
            and calls[-1][2] - calls[0][2] < 5
            and all(row["state"] == "requested" for row in environment.states())
        ):
            # 앱 이벤트 루프를 남겨 두지 않고 부모에게 실패를 전달한다.
            os._exit(1)
        print(
            json.dumps(
                {
                    "mode": "app",
                    "calls": len(calls),
                    "first_to_last_seconds": round(calls[-1][2] - calls[0][2], 3),
                    "states": environment.states(),
                    "isolated_profile": profile.identifier,
                }
            ),
            flush=True,
        )
        os.kill(os.getpid(), signal.SIGTERM)

    # 앱 설정·복원 상태·PID 경로도 임시 영역으로 돌려 기존 개발 설치에 접근하지 않는다.
    with (
        patch("src.bootstrap.build_profile.DEVELOPMENT", profile),
        patch("src.bootstrap.instance.application_directory", return_value=environment.root),
        patch("src.bootstrap.app_settings.application_directory", return_value=environment.root),
        patch("src.bootstrap.desktop.application_directory", return_value=environment.root),
        patch(
            "src.bootstrap.notification_worker.application_directory", return_value=environment.root
        ),
        patch(
            "src.infrastructure.macos_keychain.MacKeychain.read",
            return_value=CredentialRecord(1, CredentialSecret(environment.token)),
        ),
        patch(
            "src.bootstrap.desktop.build_senders",
            return_value={channel: environment.sender for channel in CHANNELS},
        ),
        patch("src.bootstrap.desktop.app_update_actions", return_value=None),
    ):
        observer = threading.Thread(target=monitor, daemon=True)
        observer.start()
        run_menubar({"LOCAL_API_PORT": str(environment.port)})
        observer.join(timeout=1)


def main() -> None:
    """같은 가상 큐로 수정 전후와 실제 개발 앱의 소진 시간을 비교한다."""
    from src.application.notifications import NotificationRunner

    if "--app-child" in sys.argv:
        run_app(
            DeliveryEnvironment(
                Path(os.environ["FIXTURE_DELIVERY_ROOT"]),
                int(os.environ["FIXTURE_DELIVERY_PORT"]),
                token=os.environ["FIXTURE_DELIVERY_TOKEN"],
            )
        )
        return

    with delivery_environment() as environment:
        environment.seed()
        with httpx.Client(trust_env=False) as client:
            assert (
                client.post(
                    f"http://127.0.0.1:{environment.port}/v1/notifications/claim"
                ).status_code
                == 401
            )
        if "--app" in sys.argv:
            # 네이티브 종료 뒤에도 부모가 서버와 임시 DB를 정리한다. 인증은 명령행에 넣지 않는다.
            subprocess.run(
                [sys.executable, "-m", "tests.fixtures.notification_delivery", "--app-child"],
                env={
                    **os.environ,
                    "FIXTURE_DELIVERY_ROOT": str(environment.root),
                    "FIXTURE_DELIVERY_PORT": str(environment.port),
                    "FIXTURE_DELIVERY_TOKEN": environment.token,
                },
                check=True,
                timeout=80,
            )
            assert environment.states() == [
                {"channel": channel, "state": "requested", "count": 6} for channel in CHANNELS
            ]
            return
        else:
            worker = NotificationWorker(
                NotificationRunner(
                    environment.queue, {channel: environment.sender for channel in CHANNELS}
                )
            )
            with patch(
                "src.bootstrap.notification_worker.application_directory",
                return_value=environment.root,
            ):
                worker.start()
                try:
                    deadline = time.monotonic() + (6 if "--baseline" in sys.argv else 10)
                    while time.monotonic() < deadline:
                        if len(environment.sender.calls) == 12:
                            break
                        time.sleep(0.02)
                finally:
                    worker.close()
        calls = environment.sender.calls
        print(
            json.dumps(
                {
                    "mode": "app" if "--app" in sys.argv else "worker",
                    "calls": len(calls),
                    "first_to_last_seconds": round(calls[-1][2] - calls[0][2], 3)
                    if calls
                    else None,
                    "states": environment.states(),
                }
            )
        )
        if "--baseline" not in sys.argv:
            assert len(calls) == 12 and len({item[0] for item in calls}) == 12
            assert calls[-1][2] - calls[0][2] < 5
            assert all(row["state"] == "requested" for row in environment.states())


if __name__ == "__main__":
    main()
