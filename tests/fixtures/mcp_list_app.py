"""운영 서비스와 분리된 실제 개발 API·MCP 프로세스를 실행한다."""

import asyncio
import os
from pathlib import Path
from unittest.mock import patch

import uvicorn

from src.bootstrap.environment import AppEnvironment
from src.bootstrap.wiring import build_api
from tests.fixtures.mcp_lists import ADMIN, prepare


async def run() -> None:
    """고유 임시 DB와 루프백 포트만 사용하고 Keychain 접근을 금지한다."""
    database, settings = await prepare(Path(os.environ["FIXTURE_DATABASE"]))
    port = int(os.environ["FIXTURE_PORT"])
    with patch(
        "src.infrastructure.macos_keychain.MacKeychain.read",
        side_effect=AssertionError("fixture forbids Keychain"),
    ):
        app = build_api(AppEnvironment(ADMIN, database=str(database.path), port=port), settings)
        await uvicorn.Server(
            uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", access_log=False)
        ).serve()


if __name__ == "__main__":
    asyncio.run(run())
