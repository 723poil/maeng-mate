"""빌드된 앱의 동봉 의존성과 빈 저장소를 독립적으로 점검한다."""

import asyncio
import json
import platform
import secrets
import socket
import subprocess
import tempfile
from importlib.metadata import version
from pathlib import Path

import httpx
import uvicorn

from src.bootstrap.environment import AppEnvironment
from src.bootstrap.executable import bundle_contents, packaged
from src.bootstrap.packaged_app import initialize_database
from src.bootstrap.resident import ResidentServer
from src.bootstrap.wiring import build_api
from src.bootstrap.work_settings import load_work_settings
from src.infrastructure.ai_skills import UserSkillInstaller
from src.infrastructure.skill_bundle import bundled_files
from src.infrastructure.sqlite.connection import Database


def check_skill_install(home: Path) -> None:
    """임시 홈에 두 AI용 스킬을 설치해 번들에서 읽은 모든 파일과 대조한다."""
    installer = UserSkillInstaller(home)
    result = installer.install("both")
    versions = installer.versions()
    if not versions.codex == versions.claude == versions.bundled == result.version:
        raise RuntimeError("두 대상의 스킬 설치 버전이 동봉 버전과 다릅니다.")
    expected = bundled_files()
    for root in (".agents", ".claude"):
        destination = home / root / "skills/maeng-mate"
        if any((destination / name).read_bytes() != data for name, data in expected.items()):
            raise RuntimeError("동봉 스킬 설치 결과가 일치하지 않습니다.")


def check_bundle() -> None:
    """운영 DB·Keychain을 읽지 않고 실제 번들의 API·DB·스킬 리소스를 확인한다."""
    from src.infrastructure.notification_app.agent import NotificationDelegate
    from src.presentation.menubar import MenuBarApp

    del MenuBarApp, NotificationDelegate
    if packaged():
        executable = (
            bundle_contents() / "Resources/NotificationAgent.app/Contents/MacOS/NotificationAgent"
        )
        result = subprocess.run(
            [str(executable)],
            input=json.dumps({"mode": "probe"}),
            capture_output=True,
            text=True,
            timeout=20,
        )
        if result.returncode or result.stdout.strip() != "ready":
            raise RuntimeError("동봉 알림 앱 확인 실패")
    with tempfile.TemporaryDirectory(prefix="maeng-bundle-check-") as directory:
        check_skill_install(Path(directory))
        path = str(Path(directory) / "app.sqlite3")
        initialize_database(path)
        token = secrets.token_urlsafe(32)

        async def request() -> None:
            """실제 루프백 서버에서 동봉 프로토콜·SDK 시작·직렬화를 확인한다."""
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
                listener.bind(("127.0.0.1", 0))
                listener.listen()
                listener.setblocking(False)
                port = listener.getsockname()[1]
                app = build_api(
                    AppEnvironment(token, port=port, database=path),
                    load_work_settings(Database(path)),
                )
                server = ResidentServer(
                    uvicorn.Config(app, log_level="critical", access_log=False), listener
                )
                serving = asyncio.create_task(server.serve())
                try:

                    async def ready() -> None:
                        """시작 실패는 대기로 숨기지 않는다."""
                        while not server.started:
                            if serving.done():
                                await serving
                                raise RuntimeError("동봉 서버 시작 실패")
                            await asyncio.sleep(0.01)

                    await asyncio.wait_for(ready(), 10)
                    async with httpx.AsyncClient(
                        base_url=f"http://127.0.0.1:{port}",
                        trust_env=False,
                        headers={"Authorization": "Bearer " + token},
                    ) as client:
                        for route in (
                            "/v1/status",
                            "/v1/preferences",
                            "/v1/day",
                            "/v1/calendar?month=2026-11",
                            "/v1/mcp/settings",
                        ):
                            response = await client.get(route)
                            if response.status_code != 200:
                                raise RuntimeError("동봉 API 확인 실패")
                finally:
                    server.should_exit = True
                    await asyncio.wait_for(serving, 10)

        asyncio.run(request())
    print(
        json.dumps(
            {
                "ok": True,
                "version": version("maeng-mate"),
                "architecture": platform.machine(),
                "api": True,
                "mcp": True,
                "sqlite": True,
                "cocoa": True,
                "skills": True,
                "notification": packaged(),
            }
        )
    )
