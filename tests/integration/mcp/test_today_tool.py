"""MCP의 날짜 입력과 읽기 포트 연결을 검증한다."""

import pytest
from mcp.server.fastmcp.exceptions import ToolError

from src.application.errors import StatusReadError
from src.application.task_search import TaskSearch
from src.presentation.mcp import create_server


class Reader:
    """원천·DB 기능 없이 로컬 조회 계약만 제공한다."""

    async def read_status(self) -> dict[str, object]:
        """상태 조회 계약을 유지한다."""
        return {"runtime": "python"}

    async def read_today(self, requested_date: str | None = None) -> dict[str, object]:
        """명시한 날짜를 그대로 조회 포트에 전달했는지 확인한다."""
        if requested_date == "2026-11-14":
            raise StatusReadError("원본은 전송하지 않는다")
        return {"date": requested_date, "tasks": [], "meetings": []}

    async def read_workday(self) -> dict[str, object]:
        """실제 근무 데이터 없이 근무 조회 호출을 확인한다."""
        return {"recorded": False}

    async def read_task_statuses(self) -> dict[str, object]:
        """소스별 선택지를 상태 도구에 그대로 전달하는지 확인한다."""
        return {"sources": [{"sourceId": "fixture", "statuses": ["검토 중"]}]}

    async def search_tasks(self, search: TaskSearch) -> dict[str, object]:
        """검증한 조건을 HTTP 어댑터에 전달할 때처럼 직렬화한다."""
        return search.payload()


async def test_today_tool_lists_and_calls_read_only_port() -> None:
    """새 도구도 읽기 전용이며 날짜를 전달하고 기본 날짜는 API에 맡긴다."""
    reader = Reader()
    server = create_server(reader)
    assert "python" in str(await server.call_tool("get_my_system_status", {}))
    assert "recorded" in str(await server.call_tool("get_my_workday", {}))
    _, catalog = await server.call_tool("list_my_task_statuses", {})
    assert catalog["sources"] == [{"sourceId": "fixture", "statuses": ["검토 중"]}]
    filters = {"source_id": "fixture", "statuses": ["검토 중"], "include_unset": True, "limit": 37}
    _, search = await server.call_tool("search_my_tasks", filters)
    assert all(search[key] == value for key, value in filters.items())
    result = await server.call_tool("get_my_today", {"date": "2026-11-13"})
    assert "2026-11-13" in str(result)
    result = await server.call_tool("get_my_today", {})
    assert "date" in str(result)


async def test_today_tool_errors_are_safe() -> None:
    """잘못된 날짜와 로컬 API 실패를 원문 없이 알린다."""
    reader = Reader()
    server = create_server(reader)
    for date in ["private-invalid-date", "2026-11-14"]:
        with pytest.raises(ToolError) as error:
            await server.call_tool("get_my_today", {"date": date})
        assert "원본은" not in str(error.value)
        assert "private-invalid-date" not in str(error.value)


async def test_task_search_preserves_http_json_types() -> None:
    """SDK 호출이 공통 파서와 같은 원본 타입과 기본값을 조회 포트로 전달한다."""
    server = create_server(Reader())
    for invalid in (
        {"limit": True},
        {"limit": 1.0},
        {"offset": "2"},
        {"include_unset": "false"},
        {"statuses": '["검토"]'},
        {"unexpected": "private-invalid-value"},
    ):
        with pytest.raises(ToolError, match="검색어·카테고리") as error:
            await server.call_tool("search_my_tasks", invalid)
        assert "private-invalid-value" not in str(error.value)

    for valid in ({}, {"query": "null"}, {"limit": 2, "offset": 1, "include_unset": False}):
        _, result = await server.call_tool("search_my_tasks", valid)
        assert result == TaskSearch.parse(valid).payload()
