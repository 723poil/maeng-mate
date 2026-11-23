"""격리된 SQLite·HTTP·실제 두 MCP 전송에서 목록 접근 경계를 검증한다."""

import asyncio
import hashlib
import json
import os
import signal
import socket
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import httpx
import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamable_http_client

from src.application.mcp_access import McpSettingsService
from src.application.mcp_lists import ListQueries
from src.bootstrap.clock import now
from src.bootstrap.environment import AppEnvironment
from src.bootstrap.mcp_list_commands import run_list_access
from src.bootstrap.wiring import build_api
from src.domain.display_columns import DisplayColumn
from src.domain.models import SourceMapping
from src.domain.source_display import SourceDisplay
from src.infrastructure.notion.source import NotionSource
from src.infrastructure.sqlite.connection import Database
from src.infrastructure.sqlite.mcp_access import SQLiteMcpAccess
from src.infrastructure.sqlite.mcp_lists import SQLiteListAccess
from src.infrastructure.sqlite.snapshots import SQLiteSnapshots
from src.infrastructure.sqlite.work_reader import SQLiteWorkReader
from tests.fixtures.mcp_lists import ADMIN, RAW_TEXT, business_state, prepare
from tests.fixtures.notion import page, task_schema


def grant(source: str, fields: list[str]) -> dict[str, object]:
    """소스 전체 행과 필요한 칼럼에만 명시적으로 동의한다."""
    return {
        "name": "fixture",
        "expiresInDays": 1,
        "allRows": True,
        "sources": [{"sourceId": source, "fields": fields}],
    }


@pytest.mark.parametrize("identity", [None, "fixture-me"])
async def test_http_lists_ignore_personal_display_and_preserve_raw_storage(
    tmp_path: Path, identity: str | None
) -> None:
    """본인 ID 없이 타인·미지정 행과 표시 생략 전 칼럼을 허용 범위에서 반환한다."""
    database, settings = await prepare(tmp_path / "lists.sqlite3")
    app = build_api(
        AppEnvironment(ADMIN, database=str(database.path), notion_user_id=identity), settings
    )
    before = await business_state(database)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://fixture",
        headers={"Authorization": f"Bearer {ADMIN}"},
    ) as client:
        assert (await client.get("/v1/status")).json()["configuration"]["personalIdentity"] is (
            identity is not None
        )
        issued = (
            await client.post(
                "/v1/mcp/list-access/tokens",
                json=grant(
                    "list-scoped",
                    [
                        "title",
                        "status",
                        "due",
                        "assignees",
                        "column:note",
                        "column:amount",
                        "column:checked",
                        "column:tags",
                    ],
                ),
            )
        ).json()
        headers = {"Authorization": "Bearer " + issued["token"]}
        response = await client.post("/v1/mcp/lists/search", headers=headers, json={"limit": 1})
        assert response.status_code == 200
        result = response.json()
        assert result["totalCount"] == 3 and result["nextOffset"] == 1
        fields = result["items"][0]["fields"]
        assert fields["column:note"]["value"] == RAW_TEXT
        assert fields["column:amount"]["value"] == 0
        assert fields["column:checked"]["value"] is False
        assert fields["column:tags"]["value"] == ["A", "B"]
        assert fields["due"]["value"]["kind"] == "instant"
        assert "url" not in fields
        for offset in (1, 2):
            result = (
                await client.post(
                    "/v1/mcp/lists/search", headers=headers, json={"offset": offset, "limit": 1}
                )
            ).json()
            assert len(result["items"]) == 1
        filtered = (
            await client.post(
                "/v1/mcp/lists/search", headers=headers, json={"assignee": "fixture-other"}
            )
        ).json()
        assert filtered["totalCount"] == 1
        catalog = (await client.get("/v1/mcp/lists/sources", headers=headers)).json()
        assert len(catalog["sources"]) == 1 and "list-ungranted" not in json.dumps(catalog)
        assert (
            await client.post(
                "/v1/mcp/lists/search", headers=headers, json={"source_id": "list-ungranted"}
            )
        ).status_code == 403
        assert (await client.get("/v1/day", headers=headers)).status_code == 401
        assert (await client.get("/v1/mcp/list-access/catalog", headers=headers)).status_code == 401
        free = (
            await client.post("/v1/mcp/list-access/tokens", json=grant("list-free", ["title"]))
        ).json()
        free_headers = {"Authorization": "Bearer " + free["token"]}
        assert (await client.post("/v1/mcp/lists/search", headers=free_headers, json={})).json()[
            "totalCount"
        ] == 3
        assert (
            await client.post(
                "/v1/mcp/lists/search", headers=free_headers, json={"assignee": "fixture-me"}
            )
        ).status_code == 422
        assert (
            await client.post(
                "/v1/mcp/lists/search", headers=free_headers, json={"statuses": ["진행"]}
            )
        ).status_code == 422
        # 기본 개인 토큰은 신규 목록 권한을 얻지 않고 신규 토큰도 개인 권한을 얻지 않는다.
        legacy = McpSettingsService(SQLiteMcpAccess(database), "http://fixture/mcp/").issue(
            "legacy", 1
        )
        assert not SQLiteMcpAccess(database).authorize(
            hashlib.sha256(free["token"].encode()).hexdigest(), 0
        )
        assert (
            await client.get(
                "/v1/mcp/lists/sources", headers={"Authorization": f"Bearer {legacy.token.value}"}
            )
        ).status_code == 401
        assert (await client.get("/v1/mcp/lists/sources")).status_code == 403
        assert (
            await client.post(
                "/v1/mcp/list-access/tokens",
                json={**grant("list-free", ["title"]), "allRows": False},
            )
        ).status_code == 422
        assert "token" not in (await client.get("/v1/mcp/list-access/tokens")).text.replace(
            '"tokens"', ""
        )
        assert (
            await client.post(f"/v1/mcp/list-access/tokens/{free['id']}/revoke")
        ).status_code == 200
        assert (await client.get("/v1/mcp/lists/sources", headers=free_headers)).status_code == 401
    assert before == await business_state(database)


async def test_concurrent_consumers_keep_distinct_fields_and_sources(tmp_path: Path) -> None:
    """같은 앱의 동시 요청도 서로 다른 소비자의 소스·칼럼을 섞지 않는다."""
    database, settings = await prepare(tmp_path / "concurrent.sqlite3")
    app = build_api(AppEnvironment(ADMIN, database=str(database.path)), settings)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://fixture"
    ) as client:
        admin = {"Authorization": f"Bearer {ADMIN}"}
        first = (
            await client.post(
                "/v1/mcp/list-access/tokens", headers=admin, json=grant("list-free", ["title"])
            )
        ).json()
        second = (
            await client.post(
                "/v1/mcp/list-access/tokens",
                headers=admin,
                json=grant("list-scoped", ["column:note"]),
            )
        ).json()
        responses = await asyncio.gather(
            client.post(
                "/v1/mcp/lists/search",
                headers={"Authorization": "Bearer " + first["token"]},
                json={},
            ),
            client.post(
                "/v1/mcp/lists/search",
                headers={"Authorization": "Bearer " + second["token"]},
                json={},
            ),
        )
        for response, source, fields in zip(
            responses, ("list-free", "list-scoped"), ({"title"}, {"column:note"}), strict=True
        ):
            assert response.status_code == 200
            assert {row["sourceId"] for row in response.json()["items"]} == {source}
            assert all(set(row["fields"]) == fields for row in response.json()["items"])


async def test_field_rebinding_old_snapshot_and_real_storage_failure(tmp_path: Path) -> None:
    """누락된 칼럼은 구별하고 실제 저장 오류는 빈 정상 결과로 바꾸지 않는다."""
    database, settings = await prepare(tmp_path / "lists.sqlite3")
    service = ListQueries(
        settings, SQLiteWorkReader(database), SQLiteListAccess(database), now, 1200
    )
    issued = service.issue(grant("list-scoped", ["title", "column:note"]))
    token = issued["token"]
    assert isinstance(token, str)
    digest = hashlib.sha256(token.encode()).hexdigest()
    source = settings.sources[0]
    changed = replace(source, stored_columns=())
    other = ListQueries(
        replace(settings, sources=(changed,)), SQLiteWorkReader(database), service.access, now, 1200
    )
    result = await other.search(digest, {})
    assert result["totalCount"] == 3
    items = result["items"]
    assert isinstance(items, list)
    assert items[0]["fields"]["column:note"]["available"] is False
    with database.transaction() as connection:
        connection.execute("UPDATE item_snapshots SET payload=json_remove(payload,'$.raw_fields')")
    missing = await service.search(digest, {})
    assert missing["items"][0]["fields"]["column:note"]["reason"] == "raw_field_not_collected"
    app = build_api(AppEnvironment(ADMIN, database=str(database.path)), settings)
    with database.transaction() as connection:
        connection.execute(
            "UPDATE item_snapshots SET payload='invalid' WHERE source_id='list-scoped'"
        )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://fixture"
    ) as client:
        result = await client.post(
            "/v1/mcp/lists/search", headers={"Authorization": f"Bearer {token}"}, json={}
        )
        assert result.status_code == 503
        assert "invalid" not in result.text


async def test_nested_list_raw_fields_keep_logical_bindings(tmp_path: Path) -> None:
    """하위 표별 실제 속성 ID도 허용한 논리 칼럼으로 다시 연결한다."""
    database, settings = await prepare(tmp_path / "rounds.sqlite3")
    source = SourceMapping(
        "task",
        "round-source",
        {"title": "Title", "status": "Status"},
        flexible=True,
        round_title_property="Round",
        stored_columns=(DisplayColumn("note", "설명", "rich_text", "Note"),),
    )
    category = settings.display[0].category
    settings = replace(
        settings,
        sources=(source,),
        display=(SourceDisplay(source.source_id, "task", "tasks", category=category),),
    )

    async def respond(request: httpx.Request) -> httpx.Response:
        """차시·하위 표·행만 가상화하고 실제 속성 이름 연결을 검증한다."""
        path = request.url.path
        if path == "/v1/data_sources/round-source":
            return httpx.Response(
                200,
                json={
                    "object": "data_source",
                    "properties": {"Round": {"id": "r", "type": "title"}},
                },
            )
        if path == "/v1/data_sources/child-source":
            schema = task_schema()
            props = schema["properties"]
            assert isinstance(props, dict)
            props.update(
                {"Title": {"id": "t", "type": "title"}, "Note": {"id": "n", "type": "rich_text"}}
            )
            return httpx.Response(200, json=schema)
        if path == "/v1/databases/child-db":
            return httpx.Response(200, json={"data_sources": [{"id": "child-source"}]})
        if path.endswith("/blocks/round-page/children"):
            rows: list[object] = [{"id": "child-db", "type": "child_database"}]
        elif path.endswith("/round-source/query"):
            rows = [{"object": "page", "id": "round-page", "properties": {}}]
        elif path.endswith("/child-source/query"):
            value = page()
            props = value["properties"]
            assert isinstance(props, dict)
            props["Note"] = {
                "id": "n",
                "type": "rich_text",
                "rich_text": [{"plain_text": RAW_TEXT}],
            }
            rows = [value]
        else:
            raise AssertionError("가상 범위 밖의 원천 요청")
        return httpx.Response(
            200, json={"object": "list", "results": rows, "has_more": False, "next_cursor": None}
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        collected = await NotionSource("fake", client, interval=0).collect(
            source, now().isoformat()
        )
    store = SQLiteSnapshots(database)
    run = await store.begin_run(source, now().isoformat())
    await store.complete_run(run, collected.items, now().isoformat())
    service = ListQueries(
        settings, SQLiteWorkReader(database), SQLiteListAccess(database), now, 1200
    )
    issued = service.issue(grant("round-source", ["column:note"]))
    token = issued["token"]
    assert isinstance(token, str)
    result = await service.search(hashlib.sha256(token.encode()).hexdigest(), {})
    assert result["items"][0]["fields"]["column:note"]["value"] == RAW_TEXT


async def wait_ready(process: subprocess.Popen[bytes], client: httpx.AsyncClient) -> None:
    """개발 API가 실제 응답할 때까지만 제한된 시간 안에서 기다린다."""
    for _ in range(100):
        assert process.poll() is None
        try:
            if (await client.get("/v1/status")).status_code == 200:
                return
        except httpx.HTTPError:
            pass
        await asyncio.sleep(0.05)
    raise AssertionError("가상 개발 앱 준비 시간 초과")


async def test_isolated_development_app_http_and_stdio_mcp(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """실제 개발 앱 프로세스에 두 MCP 전송을 연결하고 저장 결과를 대조한다."""
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    path = tmp_path / "development.sqlite3"
    env = {
        "PATH": os.defpath,
        "PYTHONPATH": str(Path.cwd()),
        "FIXTURE_DATABASE": str(path),
        "FIXTURE_PORT": str(port),
    }
    with (tmp_path / "app.log").open("wb") as log:
        process = await asyncio.to_thread(
            subprocess.Popen,
            [sys.executable, "-m", "tests.fixtures.mcp_list_app"],
            env=env,
            stdout=log,
            stderr=log,
        )
        try:
            async with httpx.AsyncClient(
                base_url=f"http://127.0.0.1:{port}",
                headers={"Authorization": f"Bearer {ADMIN}"},
                trust_env=False,
            ) as admin:
                await wait_ready(process, admin)
                await asyncio.to_thread(
                    run_list_access, {"LOCAL_API_PORT": str(port), "LOCAL_API_TOKEN": ADMIN}, None
                )
                assert len(json.loads(capsys.readouterr().out)["sources"]) == 3
                issued = (
                    await admin.post(
                        "/v1/mcp/list-access/tokens",
                        json=grant("list-free", ["title", "column:note"]),
                    )
                ).json()
                token = issued["token"]
                before = await business_state(Database(str(path)))
                async with httpx.AsyncClient(
                    headers={"Authorization": f"Bearer {token}"}, trust_env=False
                ) as transport:
                    async with streamable_http_client(
                        f"http://127.0.0.1:{port}/mcp-lists/", http_client=transport
                    ) as (read, write, _):
                        async with ClientSession(read, write) as session:
                            await session.initialize()
                            assert {tool.name for tool in (await session.list_tools()).tools} == {
                                "list_sources",
                                "search_items",
                            }
                            result = await session.call_tool("search_items", {"limit": 1})
                            assert not result.isError and result.structuredContent is not None
                            assert result.structuredContent["totalCount"] == 3
                            assert (
                                result.structuredContent["items"][0]["fields"]["column:note"][
                                    "value"
                                ]
                                == RAW_TEXT
                            )
                            assert (
                                await session.call_tool("search_items", {"assignee": "fixture-me"})
                            ).isError
                            assert (
                                await session.call_tool("search_items", {"limit": True})
                            ).isError
                parameters = StdioServerParameters(
                    command=sys.executable,
                    args=["-m", "src", "mcp"],
                    env={
                        "PATH": os.defpath,
                        "PYTHONPATH": str(Path.cwd()),
                        "LOCAL_API_PORT": str(port),
                        "MCP_ACCESS_TOKEN": token,
                        "MCP_QUERY_MODE": "lists",
                    },
                )
                async with stdio_client(parameters) as (read, write):
                    async with ClientSession(read, write) as session:
                        await session.initialize()
                        result = await session.call_tool("search_items", {"offset": 1, "limit": 1})
                        assert not result.isError and result.structuredContent is not None
                        assert result.structuredContent["nextOffset"] == 2
                        assert len(result.structuredContent["items"]) == 1
                assert before == await business_state(Database(str(path)))
                # 저장 경계를 실제로 손상시킨 뒤 두 전송이 모두 오류를 보존하는지 확인한다.
                with Database(str(path)).transaction() as connection:
                    connection.execute(
                        "UPDATE item_snapshots SET payload='invalid' WHERE source_id='list-free'"
                    )
                    connection.execute(
                        "UPDATE work_source_state SET generation=generation+1 WHERE source_id='list-free'"
                    )
                async with httpx.AsyncClient(
                    headers={"Authorization": f"Bearer {token}"}, trust_env=False
                ) as transport:
                    async with streamable_http_client(
                        f"http://127.0.0.1:{port}/mcp-lists/", http_client=transport
                    ) as (read, write, _):
                        async with ClientSession(read, write) as session:
                            await session.initialize()
                            assert (await session.call_tool("search_items", {})).isError
                async with stdio_client(parameters) as (read, write):
                    async with ClientSession(read, write) as session:
                        await session.initialize()
                        assert (await session.call_tool("search_items", {})).isError
        finally:
            process.terminate()
            await asyncio.to_thread(process.wait, 10)
    # Uvicorn은 정상 수명 정리 뒤 받은 종료 신호를 다시 전달할 수 있다.
    assert process.returncode in (0, -signal.SIGTERM)
