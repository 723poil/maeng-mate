"""격리된 개발 API에서 개인 MCP 검색과 메인 카드의 마감일 분류를 대조한다."""

import asyncio
import socket
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

import httpx
import uvicorn
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from src.application.mcp_access import McpSettingsService
from src.application.work_settings import Settings
from src.bootstrap.environment import AppEnvironment
from src.bootstrap.wiring import build_api
from src.domain.category_configuration import CategoryConfiguration
from src.domain.main_list import ListFilter, MainListRules
from src.domain.models import Period, Task
from src.domain.source_display import SourceCategory, SourceDisplay
from src.infrastructure.sqlite.connection import Database
from src.infrastructure.sqlite.mcp_access import SQLiteMcpAccess
from src.infrastructure.sqlite.schema import migrate
from src.infrastructure.sqlite.snapshots import SQLiteSnapshots
from tests.fixtures.mcp_lists import ADMIN
from tests.fixtures.notion import OBSERVED, SOURCE


async def test_personal_deadlines_across_reads(tmp_path: Path) -> None:
    """종료 생략·기간 종료·서울 자정 전환과 마감 시각 경계의 검색·목록 결과를 확인한다."""
    database = Database(str(tmp_path / "deadlines.sqlite3"))
    await migrate(database)
    store = SQLiteSnapshots(database)
    periods = {
        "single": Period("date", "2026-11-14", None),
        "range": Period("date", "2026-11-12", "2026-11-14"),
        "elapsed": Period("instant", "2026-11-12T15:00:00Z", "2026-11-13T15:00:00Z"),
        "exact": Period("instant", "2026-11-13T16:00:00Z", None),
        "later": Period("instant", "2026-11-13T17:00:00Z", None),
        "previous": Period("date", "2026-11-13", None),
        "unset": None,
    }
    items = [
        Task(
            key,
            SOURCE.source_id,
            "https://fixture.invalid",
            OBSERVED,
            OBSERVED,
            key,
            ("me",),
            "완료",
            due,
        )
        for key, due in periods.items()
    ]
    run = await store.begin_run(SOURCE, OBSERVED)
    await store.complete_run(run, items, OBSERVED)
    rules = MainListRules(filters=(ListFilter("completedTaskAge", "eq", "0"),))
    category = SourceCategory("fixture", "검증", CategoryConfiguration(main_list=rules))
    settings = Settings(
        (SOURCE,),
        completed_statuses={SOURCE.source_id: ("완료",)},
        display=(SourceDisplay(SOURCE.source_id, "task", "tasks", category=category),),
    )
    day = "2026-11-14"
    expected = {"single", "range", "elapsed", "exact", "later"}
    # 포트를 먼저 점유해 병렬 검증에서도 같은 주소를 두 서버가 선택하지 않게 한다.
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
        url = f"http://127.0.0.1:{port}"
        environment = AppEnvironment(
            ADMIN, database=str(database.path), notion_user_id="me", port=port
        )
        with patch(
            "src.bootstrap.wiring.now", return_value=datetime.fromisoformat("2026-11-13T16:00:00Z")
        ):
            server = uvicorn.Server(
                uvicorn.Config(build_api(environment, settings), log_level="warning")
            )
            running = asyncio.create_task(server.serve(sockets=[listener]))
            try:
                async with asyncio.timeout(10):
                    while not server.started:
                        if running.done():
                            await running
                            raise AssertionError("개발 API 시작 실패")
                        await asyncio.sleep(0.01)
                endpoint = url + "/mcp/"
                token = McpSettingsService(SQLiteMcpAccess(database), endpoint).issue("fixture", 1)
                async with httpx.AsyncClient(
                    headers={"Authorization": f"Bearer {token.token.value}"}, trust_env=False
                ) as transport:
                    async with streamable_http_client(endpoint, http_client=transport) as (
                        read,
                        write,
                        _,
                    ):
                        async with ClientSession(read, write) as session:
                            await session.initialize()
                            search = await session.call_tool(
                                "search_my_tasks", {"due_from": day, "due_to": day}
                            )
                            today = await session.call_tool("get_my_today", {"date": day})
                assert not search.isError and search.structuredContent is not None
                assert {row["id"] for row in search.structuredContent["tasks"]} == expected
                assert not today.isError and today.structuredContent is not None
                rows = today.structuredContent["tasks"]
                assert {row["id"] for row in rows if row["dueToday"]} == expected
                assert {row["id"] for row in rows if row["overdue"]} == {"previous", "elapsed"}
                async with httpx.AsyncClient(
                    base_url=url, headers={"Authorization": f"Bearer {ADMIN}"}, trust_env=False
                ) as admin:
                    response = await admin.get("/v1/today", params={"desktop": "true", "date": day})
                    response.raise_for_status()
                    card = response.json()["categoryAreas"][category.key]
                    assert {row["id"] for row in card["items"]} == expected
            finally:
                server.should_exit = True
                await running
