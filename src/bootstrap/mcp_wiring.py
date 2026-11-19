"""MCP 조회 서버를 로컬 API 연결 정보만으로 조립한다."""

from http.cookiejar import CookieJar, DefaultCookiePolicy

import httpx
from mcp.server.fastmcp import FastMCP

from src.bootstrap.environment import McpEnvironment
from src.infrastructure.local_api import LocalStatusReader
from src.presentation.mcp import create_server


def build_mcp(environment: McpEnvironment) -> tuple[FastMCP, httpx.AsyncClient]:
    """조회 서버와 전송 방식의 수명 동안 사용할 루프백 연결을 함께 조립한다."""
    client = httpx.AsyncClient(
        base_url=environment.api_url,
        headers={"Authorization": f"Bearer {environment.api_token}"},
        # 연결을 재사용해도 한 도구 요청의 응답 쿠키가 다음 요청으로 넘어가지 않는다.
        cookies=CookieJar(DefaultCookiePolicy(allowed_domains=())),
        trust_env=False,
        follow_redirects=False,
        timeout=10,
    )
    return create_server(LocalStatusReader(client)), client


async def run_stdio_mcp(environment: McpEnvironment) -> None:
    """stdio 세션이 끝나거나 취소되면 열린 조회 연결도 함께 닫는다."""
    server, client = build_mcp(environment)
    async with client:
        await server.run_stdio_async()
