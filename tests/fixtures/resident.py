"""임시 앱 경로와 가상 원천에서 실제 run 명령을 실행하는 하위 프로세스다."""

import asyncio
import os
import sys
from contextlib import ExitStack
from datetime import datetime
from functools import partial
from pathlib import Path
from unittest.mock import patch

import httpx

from src.bootstrap import instance
from src.bootstrap.cli import main
from tests.fixtures.notion import task_schema


async def respond(request: httpx.Request) -> httpx.Response:
    """Notion 외부 통신을 차단하고 성공·종료 중 취소를 재현한다."""
    assert request.url.host == "api.notion.com"
    if os.environ.get("FAKE_SLOW_SOURCE") == "1":
        await asyncio.sleep(3600)
    if request.method == "GET" and request.url.path.startswith("/v1/data_sources/"):
        return httpx.Response(200, json=task_schema())
    return httpx.Response(
        200, json={"object": "list", "results": [], "has_more": False, "next_cursor": None}
    )


class FakeClient(httpx.AsyncClient):
    """프로세스 안의 Notion HTTP 경계만 가상화한다."""

    def __init__(self, *, trust_env: bool, follow_redirects: bool) -> None:
        """실제 토큰이나 네트워크가 필요 없는 전송을 연결한다."""
        super().__init__(
            transport=httpx.MockTransport(respond),
            trust_env=trust_env,
            follow_redirects=follow_redirects,
        )


def start() -> None:
    """설정·CLI·수명 감독은 실제 구현으로 실행한다."""
    support = Path(os.environ["DATABASE_PATH"]).parent / "app-support"
    command = os.environ.get("FAKE_COMMAND", "run")
    if command not in {"run", "api"}:
        raise ValueError("격리 실행은 run 또는 api만 허용합니다.")
    with ExitStack() as isolated:
        # 실제 로그인 서비스의 역할 잠금이나 사용자 Keychain을 조회하지 않는다.
        isolated.enter_context(
            patch.object(instance, "application_directory", return_value=support)
        )
        isolated.enter_context(
            patch.object(instance, "state_directory", return_value=support / "run")
        )
        if sys.platform == "darwin":
            from src.infrastructure.macos_keychain import MacKeychain

            isolated.enter_context(patch.object(MacKeychain, "read", return_value=None))
        # 수집기가 만드는 외부 연결만 가상화하고 MCP의 내부 루프백 연결은 그대로 둔다.
        isolated.enter_context(patch("src.bootstrap.resident.httpx", AsyncClient=FakeClient))
        if instant := os.environ.get("FAKE_NOW"):
            from src.bootstrap import work_rules
            from src.infrastructure.sqlite.day_records import SQLiteDayStore

            now = datetime.fromisoformat(instant)
            # 자정이나 퇴근 시각에 관계없이 같은 오늘 기록·미래 예약을 검증한다.
            isolated.enter_context(
                patch.object(work_rules, "SQLiteDayStore", partial(SQLiteDayStore, now=lambda: now))
            )
        isolated.enter_context(patch.object(sys, "argv", ["maeng-mate", command]))
        main()


if __name__ == "__main__":
    start()
