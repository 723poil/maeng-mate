"""MCP 프로토콜과 조회 포트를 연결한다."""

from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from mcp.types import ToolAnnotations

from src.application.date_inputs import parse_day
from src.application.errors import InvalidDateError, StatusReadError
from src.application.ports import McpQueryReader
from src.application.task_search import TaskSearch, TaskSearchInputError


def create_server(reader: McpQueryReader) -> FastMCP:
    """stdio와 상주 HTTP에 같은 조회 도구와 입력 계약을 제공한다."""
    server = FastMCP(
        "maeng-mate",
        host="127.0.0.1",
        stateless_http=True,
        json_response=True,
        streamable_http_path="/",
        log_level="WARNING",
    )

    read_only = ToolAnnotations(
        readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False
    )

    @server.tool(annotations=read_only)
    async def get_my_system_status() -> dict[str, object]:
        """상주 앱의 구현·설정 상태를 읽는다. 수집과 알림은 시작하지 않는다."""
        try:
            return await reader.read_status()
        except StatusReadError:
            raise ToolError("로컬 업무 앱 조회에 실패했습니다.") from None

    @server.tool(annotations=read_only)
    async def get_my_today(date: str | None = None) -> dict[str, object]:
        """본인의 업무·QA·해당일 회의·휴가·기타 일정을 읽는다. 취소 건은 제외한다. 날짜 생략 시 서울의 오늘이며 휴가 approvalState는 승인 판단을 뜻하지 않는다. stale·warnings·completionState를 확인하고 과거 상태 복원으로 해석하지 않는다."""
        try:
            if date is not None:
                parse_day(date)
            return await reader.read_today(date)
        except InvalidDateError:
            raise ToolError("날짜는 유효한 YYYY-MM-DD 형식이어야 합니다.") from None
        except StatusReadError:
            raise ToolError(
                "개인 업무 조회에 실패했습니다. 상주 앱의 설정과 저장 상태를 확인하세요."
            ) from None

    @server.tool(annotations=read_only)
    async def get_my_workday() -> dict[str, object]:
        """서울의 오늘 출근·휴가·퇴근 예정을 읽는다. recorded=false는 미기록이며 attendanceTime이 null이면 출근을 추정하지 않는다. actualDepartureAt은 저장한 실제 퇴근이고 expectedDepartureAt은 예상 시각이다. calculationState·workRulesConfigured·warnings를 함께 확인한다. weekend_manual과 non_workday는 출퇴근을 직접 입력할 수 있는 휴무일이며 예정시간과 자동 퇴근 알림은 계산하지 않는다. overtime은 추가근무 수동 체크이며 extraWorkLabel로 야근·주말근무를 구분한다. 저장·수집·알림은 시작하지 않는다."""
        try:
            return await reader.read_workday()
        except StatusReadError:
            raise ToolError(
                "오늘 근무 조회에 실패했습니다. 로컬 앱 연결과 DB 상태를 확인하세요."
            ) from None

    @server.tool(annotations=read_only)
    async def search_my_tasks(
        query: str | None = None,
        status: str | None = None,
        due_from: str | None = None,
        due_to: str | None = None,
        section: str = "all",
        limit: int = 50,
        offset: int = 0,
    ) -> dict[str, object]:
        """현재 본인 업무·QA를 제목 부분검색·상태·마감 범위로 찾는다. 조건은 AND이며 날짜는 서울 YYYY-MM-DD 양끝 포함, 이번 주는 월~일로 지정한다. 상태는 원문 또는 진행중·시작전·리뷰·보류·완료를 쓸 수 있다. section은 tasks·qa·all이다. 결과 수 제한 전에 검색하며 nextOffset이 있으면 이어 읽는다. 기존 표시 규칙과 stale·warnings·completionState를 확인하고 빈 결과를 미수집과 혼동하지 않는다."""
        try:
            search = TaskSearch.parse(
                {
                    "query": query,
                    "status": status,
                    "due_from": due_from,
                    "due_to": due_to,
                    "section": section,
                    "limit": limit,
                    "offset": offset,
                }
            )
            return await reader.search_tasks(search)
        except TaskSearchInputError as error:
            raise ToolError(str(error)) from None
        except StatusReadError:
            raise ToolError(
                "업무 검색에 실패했습니다. 로컬 앱 연결과 수집 상태를 확인하세요."
            ) from None

    return server
