"""공식 SDK로 Python MCP의 실제 stdio 초기화와 도구 목록을 검증한다."""

import sys

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


async def test_stdio_mcp_lists_read_only_tool() -> None:
    """MCP를 연결해도 DB·Notion 토큰이나 상주 작업이 필요하지 않다."""
    parameters = StdioServerParameters(
        command=sys.executable,
        args=["-m", "src", "mcp"],
        env={
            "LOCAL_API_PORT": "43127",
            "LOCAL_API_TOKEN": "fake-" * 10,
        },
    )
    async with stdio_client(parameters) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = await session.list_tools()
            assert {tool.name for tool in tools.tools} == {
                "get_my_system_status",
                "get_my_today",
                "get_my_workday",
                "list_my_task_statuses",
                "search_my_tasks",
            }
            for tool in tools.tools:
                assert tool.annotations is not None
                assert tool.annotations.readOnlyHint is True
                assert tool.annotations.destructiveHint is False
                assert tool.annotations.idempotentHint is True
                assert tool.annotations.openWorldHint is False


async def test_stdio_queries_share_connection_and_close_it_on_exit() -> None:
    """연속 도구 호출은 하나의 루프백 연결을 쓰고 각 요청에 독립적으로 인증한다."""
    import asyncio

    connections = 0
    requests: list[bytes] = []
    paths: list[bytes] = []
    closed = asyncio.Event()

    async def serve(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        """가상 상태를 반환하고 전송이 끝난 시점까지 실제 HTTP 연결을 관찰한다."""
        nonlocal connections
        connections += 1
        try:
            while line := await reader.readline():
                paths.append(line)
                headers = await reader.readuntil(b"\r\n\r\n")
                requests.append(headers)
                writer.write(
                    b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
                    b"Set-Cookie: fixture=value; Path=/\r\nContent-Length: 16\r\n\r\n"
                    b'{"fixture":true}'
                )
                await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()
            closed.set()

    async with await asyncio.start_server(serve, "127.0.0.1", 0) as api:
        token = "fake-" * 10
        parameters = StdioServerParameters(
            command=sys.executable,
            args=["-m", "src", "mcp"],
            env={"LOCAL_API_PORT": str(api.sockets[0].getsockname()[1]), "MCP_ACCESS_TOKEN": token},
        )
        async with stdio_client(parameters) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                for tool_name in ("get_my_system_status", "list_my_task_statuses"):
                    result = await session.call_tool(tool_name, {})
                    assert not result.isError
                    assert result.structuredContent == {"fixture": True}
        await asyncio.wait_for(closed.wait(), timeout=3)
    assert connections == 1 and len(requests) == 2
    assert paths == [b"GET /v1/status HTTP/1.1\r\n", b"GET /v1/tasks/statuses HTTP/1.1\r\n"]
    for headers in requests:
        assert f"Authorization: Bearer {token}\r\n".encode() in headers
        assert b"\ncookie:" not in headers.lower()
