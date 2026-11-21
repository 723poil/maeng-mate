"""목록 전용 MCP를 기존 개인 도구와 별도 주소·계약으로 제공한다."""

import hashlib

from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import StrictBool, StrictInt

from src.application.errors import QueryReadError, SnapshotError, StatusReadError
from src.application.mcp_lists import ListAccessError, ListQueryReader
from src.application.task_search import TaskSearchInputError


def create_list_server(reader: ListQueryReader) -> FastMCP:
    """내장 HTTP 호출자와 stdio 전용 연결의 허용 범위를 각각 유지한다."""
    server = FastMCP(
        "maeng-mate-lists",
        host="127.0.0.1",
        stateless_http=True,
        json_response=True,
        streamable_http_path="/",
        log_level="WARNING",
    )
    annotations = ToolAnnotations(
        readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False
    )

    def digest() -> str | None:
        """HTTP 요청 인증을 직접 전달하며 내부 관리자 자격으로 대체하지 않는다."""
        request = server.get_context().request_context.request
        if request is None:
            return None
        header = request.headers.get("Authorization", "")
        return (
            hashlib.sha256(header.removeprefix("Bearer ").encode()).hexdigest()
            if header.startswith("Bearer ")
            else None
        )

    async def execute(payload: object | None) -> dict[str, object]:
        """오류는 실패로 보존하고 비밀값·저장 원문은 도구 메시지에 넣지 않는다."""
        try:
            return (
                await reader.catalog(digest())
                if payload is None
                else await reader.search(digest(), payload)
            )
        except ListAccessError:
            raise ToolError("목록 조회 권한을 확인하세요.") from None
        except TaskSearchInputError:
            raise ToolError("허용된 칼럼과 검색 조건을 확인하세요.") from None
        except (QueryReadError, SnapshotError, StatusReadError):
            raise ToolError("저장된 목록 자료를 조회할 수 없습니다.") from None

    @server.tool(annotations=annotations)
    async def list_sources() -> dict[str, object]:
        """명시적으로 허용한 목록 소스·카테고리·칼럼과 값 표현을 읽는다. 앱의 내 항목만 보기·개인 ID는 적용하지 않는다."""
        return await execute(None)

    @server.tool(annotations=annotations)
    async def search_items(
        source_id: str | None = None,
        category_id: str | None = None,
        query: str | None = None,
        statuses: list[str] | None = None,
        include_unset: StrictBool = False,
        due_from: str | None = None,
        due_to: str | None = None,
        assignee: str | None = None,
        limit: StrictInt = 50,
        offset: StrictInt = 0,
    ) -> dict[str, object]:
        """허용한 목록의 구조화된 값을 최대 100개씩 읽는다. 담당자 조건은 assignees 칼럼이 허용됐을 때 명시적으로만 사용한다. 원형 값이 아직 수집되지 않은 칼럼은 available=false다. nextOffset·stale·configuration을 확인한다."""
        return await execute(
            {
                "source_id": source_id,
                "category_id": category_id,
                "query": query,
                "statuses": statuses,
                "include_unset": include_unset,
                "due_from": due_from,
                "due_to": due_to,
                "assignee": assignee,
                "limit": limit,
                "offset": offset,
            }
        )

    return server
