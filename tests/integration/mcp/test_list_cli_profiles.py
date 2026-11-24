"""임시 개발·배포 프로필에서 실제 CLI 발급 수명과 stdio를 검증한다."""

import asyncio
import json
import os
import plistlib
import signal
import socket
import sys
from pathlib import Path

import httpx
import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from tests.fixtures.mcp_lists import ADMIN


async def command(env: dict[str, str], *args: str) -> dict[str, object]:
    """명령행에 인증 원문 없이 CLI 출력을 메모리에서만 검증한다."""
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "tests.fixtures.mcp_list_cli",
        *args,
        env=env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await asyncio.wait_for(process.communicate(), 15)
    assert process.returncode == 0, stderr.decode()
    value: object = json.loads(stdout)
    assert isinstance(value, dict)
    return {key: item for key, item in value.items() if isinstance(key, str)}


@pytest.mark.parametrize("profile", ["development", "release"])
async def test_cli_lifecycle_with_temporary_profiles(tmp_path: Path, profile: str) -> None:
    """CLI 원문 출력·비밀값 없는 목록·폐기와 두 프로필의 소비자 연결을 확인한다."""
    root, database = tmp_path / profile, tmp_path / "data.sqlite3"
    root.mkdir(mode=0o700)
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    bootstrap = root / "bootstrap.json"
    bootstrap.write_text(json.dumps({"version": 2, "databasePath": str(database), "port": port}))
    bootstrap.chmod(0o600)
    contents = root / "Catch.app/Contents"
    contents.mkdir(parents=True)
    (contents / "Info.plist").write_bytes(plistlib.dumps({"SUPublicEDKey": "fixture-public-key"}))
    env = {
        "PATH": os.defpath,
        "PYTHONPATH": str(Path.cwd()),
        "FIXTURE_DATABASE": str(database),
        "FIXTURE_PORT": str(port),
        "FIXTURE_PROFILE_ROOT": str(root),
        "FIXTURE_PROFILE": profile,
    }
    app = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "tests.fixtures.mcp_list_app",
        env=env,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        async with httpx.AsyncClient(
            base_url=f"http://127.0.0.1:{port}",
            trust_env=False,
            headers={"Authorization": f"Bearer {ADMIN}"},
        ) as client:
            for _ in range(100):
                assert app.returncode is None
                try:
                    if (await client.get("/v1/status")).status_code == 200:
                        break
                except httpx.HTTPError:
                    pass
                await asyncio.sleep(0.05)
            else:
                raise AssertionError("격리 개발 API 준비 시간 초과")
        # CLI 환경에 포트·관리 키를 주입하지 않아 실제 bootstrap/Keychain 경계를 거친다.
        catalog = await command(env, "settings", "mcp-lists")
        assert len(catalog["sources"]) == 3
        request = root / "issue.json"
        request.write_text(
            json.dumps(
                {
                    "action": "issue",
                    "name": "fixture",
                    "expiresInDays": 1,
                    "allRows": True,
                    "sources": [{"sourceId": "list-free", "fields": ["title"]}],
                }
            )
        )
        issued = await command(env, "settings", "mcp-lists", "--file", str(request))
        token, identifier = issued["token"], issued["id"]
        assert isinstance(token, str) and isinstance(identifier, str)
        request.write_text(json.dumps({"action": "tokens"}))
        listed = await command(env, "settings", "mcp-lists", "--file", str(request))
        assert token not in json.dumps(listed) and ADMIN not in json.dumps(listed)
        assert any(row["id"] == identifier for row in listed["tokens"])
        parameters = StdioServerParameters(
            command=sys.executable,
            args=["-m", "tests.fixtures.mcp_list_cli", "mcp"],
            env={
                **env,
                "MCP_QUERY_MODE": "lists",
                "MCP_ACCESS_TOKEN": token,
                "LOCAL_API_PORT": str(port),
            },
        )
        async with stdio_client(parameters) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                result = await session.call_tool("search_items", {"limit": 1})
                assert not result.isError and result.structuredContent is not None
                assert result.structuredContent["totalCount"] == 3
        request.write_text(json.dumps({"action": "revoke", "id": identifier}))
        assert await command(env, "settings", "mcp-lists", "--file", str(request)) == {
            "revoked": True
        }
        async with stdio_client(parameters) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                assert (await session.call_tool("search_items", {})).isError
    finally:
        app.terminate()
        await asyncio.wait_for(app.wait(), 10)
    assert app.returncode in (0, -signal.SIGTERM)
