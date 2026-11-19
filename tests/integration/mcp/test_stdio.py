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
                "search_my_tasks",
            }
            for tool in tools.tools:
                assert tool.annotations is not None
                assert tool.annotations.readOnlyHint is True
                assert tool.annotations.destructiveHint is False
                assert tool.annotations.idempotentHint is True
                assert tool.annotations.openWorldHint is False
