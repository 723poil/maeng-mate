"""실제 루프백 서버와 Cocoa 설정 창을 격리 자료로 실행하는 수동 PDD다."""

import json
import os
import secrets
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import httpx
from AppKit import NSApplication, NSApplicationActivationPolicyAccessory
from Foundation import NSDate, NSRunLoop

from src.infrastructure.notion_credentials_api import NotionCredentialsApi
from src.infrastructure.preferences_api import PreferencesApi
from src.infrastructure.runtime_settings_api import RuntimeSettingsApi
from src.infrastructure.slack_settings_api import SlackSettingsApi
from src.presentation.notion_credentials_controller import NotionCredentialsController
from src.presentation.preferences_controller import PreferencesController
from src.presentation.preferences_sidebar import SettingsPage
from src.presentation.preferences_window import PreferencesWindow
from src.presentation.runtime_settings_controller import RuntimeSettingsController
from src.presentation.slack_settings_controller import SlackSettingsController
from tests.fixtures.credential_errors import credential_files, database_rows


def member_saved() -> None:
    """가상 수신자 저장 뒤 다른 화면이나 운영 상태를 갱신하지 않는다."""


def settle(
    window: PreferencesWindow,
    preferences: PreferencesController,
    runtime: RuntimeSettingsController,
    notion: NotionCredentialsController,
    slack: SlackSettingsController,
) -> None:
    """실제 Cocoa 루프를 돌리면서 API 작업 완료와 렌더링을 기다린다."""
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        preferences.tick()
        runtime.tick()
        notion.tick()
        slack.tick()
        window.render()
        NSRunLoop.currentRunLoop().runUntilDate_(NSDate.dateWithTimeIntervalSinceNow_(0.02))
        if not any((preferences.busy, runtime.busy, notion.busy, slack.busy)):
            return
    raise AssertionError("격리 개발 UI 작업 시간 초과")


def snapshot(window: PreferencesWindow, path: Path) -> None:
    """실제 Cocoa 설정 창을 PNG로 남기고 토큰 입력이 비어 있는지 확인한다."""
    # 유리 효과의 GPU 합성은 NSView 캐시에 담기지 않으므로 창 번호로 캡처한다.
    subprocess.run(
        ["/usr/sbin/screencapture", "-x", "-l", str(window._window.windowNumber()), str(path)],
        check=True,
    )


def validate_ui(root: Path, address: str, admin: str) -> list[str]:
    """실제 설정 카드의 버튼을 눌러 성공·충돌·일시 장애와 삭제를 확인한다."""
    application = NSApplication.sharedApplication()
    application.setActivationPolicy_(NSApplicationActivationPolicyAccessory)
    preferences = PreferencesController(PreferencesApi(address, admin))
    runtime = RuntimeSettingsController(RuntimeSettingsApi(address, admin))
    notion = NotionCredentialsController(NotionCredentialsApi(address, admin))
    slack = SlackSettingsController(SlackSettingsApi(address, admin), member_saved)
    window = PreferencesWindow(
        preferences,
        (),
        runtime_controller=runtime,
        credentials_controller=notion,
        slack_controller=slack,
    )
    # 로그인 서비스·기본 지원 폴더 없이 실제 제품 설정 창만 띄운다.
    window._window.setTitle_("Maeng Mate · #49 isolated PDD")
    window._window.orderFront_(None)
    results: list[str] = []
    try:
        settle(window, preferences, runtime, notion, slack)
        notion_view, slack_view = window._credentials_view, window._slack_view
        assert notion_view is not None and slack_view is not None
        window._sidebar.select(SettingsPage.NOTION)
        assert not notion_view.check.isEnabled()
        window._sidebar.select(SettingsPage.SLACK)
        settle(window, preferences, runtime, notion, slack)
        assert not slack_view._check.isEnabled()
        results.append("미등록 두 카드의 연결 확인 비활성화")
        with httpx.Client(
            base_url=address, trust_env=False, headers={"Authorization": f"Bearer {admin}"}
        ) as client:
            for base, method, save in (
                ("/v1/notion/credentials", "PUT", "/v1/notion/credentials"),
                ("/v1/slack/settings", "POST", "/v1/slack/settings/token"),
            ):
                response = client.post(base + "/check", json={"revision": 0})
                assert response.status_code == 422 and response.json() == {
                    "detail": {"code": "missing"}
                }
                assert (
                    client.request(
                        method, save, json={"revision": 0, "token": secrets.token_urlsafe(48)}
                    ).status_code
                    == 200
                )
            results.append("실제 루프백 미등록 422/missing 및 가상 토큰 저장")
            notion.reload()
            slack.reload()
            settle(window, preferences, runtime, notion, slack)
            for page, button, controller in (
                (SettingsPage.NOTION, notion_view.check, notion),
                (SettingsPage.SLACK, slack_view._check, slack),
            ):
                window._sidebar.select(page)
                settle(window, preferences, runtime, notion, slack)
                assert button.isEnabled()
                button.performClick_(None)
                settle(window, preferences, runtime, notion, slack)
                assert controller.state is not None and controller.state.verification == "ok"
                assert controller.error_code is None
            results.append("실제 Cocoa 연결 확인 버튼 및 정상 인증 배지")
            stored, credentials = database_rows(root), credential_files(root)
            (root / "failure").write_text("timeout")
            for page, button, controller, name in (
                (SettingsPage.NOTION, notion_view.check, notion, "notion"),
                (SettingsPage.SLACK, slack_view._check, slack, "slack"),
            ):
                window._sidebar.select(page)
                settle(window, preferences, runtime, notion, slack)
                button.performClick_(None)
                settle(window, preferences, runtime, notion, slack)
                assert controller.error_code == "timeout" and controller.editable
                assert not notion_view.token.stringValue() and not slack_view.token.stringValue()
                snapshot(window, root / f"{name}-timeout.png")
            assert stored == database_rows(root) and credentials == credential_files(root)
            (root / "failure").unlink()
            results.append("Cocoa timeout 안내·재시도 가능 및 SQLite/자격 정보 파일 보존")
            # 다른 화면이 갱신한 버전을 실제 HTTP 저장으로 만들어 오래된 UI를 재현한다.
            assert (
                client.put(
                    "/v1/notion/credentials",
                    json={"revision": 1, "token": secrets.token_urlsafe(48)},
                ).status_code
                == 200
            )
            window._sidebar.select(SettingsPage.NOTION)
            notion_view.check.performClick_(None)
            settle(window, preferences, runtime, notion, slack)
            assert notion.error_code == "conflict" and notion.blocked
            snapshot(window, root / "notion-conflict.png")
            window._sidebar.select(SettingsPage.SLACK)
            settle(window, preferences, runtime, notion, slack)
            assert (
                client.post(
                    "/v1/slack/settings/token",
                    json={"revision": 1, "token": secrets.token_urlsafe(48)},
                ).status_code
                == 200
            )
            slack_view._check.performClick_(None)
            settle(window, preferences, runtime, notion, slack)
            assert slack.error_code == "conflict" and slack.error is not None
            assert "다시 불러" in slack.error
            snapshot(window, root / "slack-conflict.png")
            results.append("두 Cocoa 컨트롤러의 오래된 버전 충돌 및 다시 읽기 안내")
            notion.reload()
            slack.reload()
            settle(window, preferences, runtime, notion, slack)
            for page, button, controller in (
                (SettingsPage.NOTION, notion_view.delete, notion),
                (SettingsPage.SLACK, slack_view._delete, slack),
            ):
                window._sidebar.select(page)
                settle(window, preferences, runtime, notion, slack)
                button.performClick_(None)
                settle(window, preferences, runtime, notion, slack)
                assert controller.state is not None and not controller.state.configured
            results.append("두 Cocoa 삭제 버튼과 저장된 삭제 표시")
        return results
    finally:
        window._window.orderOut_(None)
        window.dispose()
        for controller in (preferences, runtime, notion, slack):
            controller.close()


def main() -> None:
    """고유 임시 프로필·포트와 프로세스를 만들고 종료 후 비밀값을 제거한다."""
    root = Path(tempfile.mkdtemp(prefix="maeng-mate-49-pdd-"))
    admin = secrets.token_urlsafe(48)
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    address = f"http://127.0.0.1:{port}"
    env = {
        "PATH": os.defpath,
        "PYTHONPATH": str(Path.cwd()),
        "FIXTURE_ROOT": str(root),
        "FIXTURE_PORT": str(port),
        "LOCAL_API_TOKEN": admin,
    }
    server = subprocess.Popen(
        [sys.executable, "-m", "tests.fixtures.credential_errors"],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        with httpx.Client(
            base_url=address, trust_env=False, headers={"Authorization": f"Bearer {admin}"}
        ) as client:
            deadline = time.monotonic() + 10
            while True:
                assert server.poll() is None, "격리 API 시작 실패"
                try:
                    if client.get("/v1/status").status_code == 200:
                        break
                except httpx.HTTPError:
                    pass
                assert time.monotonic() < deadline, "격리 API 준비 시간 초과"
                time.sleep(0.05)
        results = validate_ui(root, address, admin)
        (root / "report.json").write_text(
            json.dumps({"port": port, "results": results}, ensure_ascii=False, indent=2)
        )
        print(f"PDD passed: {root / 'report.json'}")
    finally:
        server.terminate()
        server.wait(timeout=10)
        # 화면 증거와 일반 SQLite만 남기고 가상 토큰 파일도 폐기한다.
        for name in ("notion.json", "slack.json", "slack-app.json"):
            (root / name).unlink(missing_ok=True)


if __name__ == "__main__":
    main()
