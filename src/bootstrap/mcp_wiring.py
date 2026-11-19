"""MCP 조회 서버를 로컬 API 연결 정보만으로 조립한다."""

from mcp.server.fastmcp import FastMCP

from src.bootstrap.environment import McpEnvironment
from src.infrastructure.local_api import LocalStatusReader
from src.presentation.mcp import create_server


def build_mcp(environment: McpEnvironment) -> FastMCP:
    """저장소와 외부 서비스 없이 개인 조회 도구를 연결한다."""
    return create_server(LocalStatusReader(environment.api_url, environment.api_token))
