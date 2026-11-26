"""MCP 프로토콜과 조회 포트를 연결한다."""

from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from mcp.server.fastmcp.tools import Tool
from mcp.server.fastmcp.utilities.func_metadata import FuncMetadata
from mcp.types import ToolAnnotations
from pydantic import ValidationError

from src.application.date_inputs import parse_day
from src.application.errors import InvalidDateError, StatusReadError
from src.application.ports import McpQueryReader
from src.application.task_search import TaskSearch, TaskSearchInputError


class SearchArguments(FuncMetadata):
    """SDK가 문자열을 JSON으로 다시 해석하기 전에 원본 검색 필드를 검사한다."""

    def pre_parse_json(self, data: dict[str, object]) -> dict[str, object]:
        """도구 시그니처로 타입과 키를 검사하고 의미 검증에는 원래 값을 넘긴다."""
        try:
            self.arg_model.model_validate(data, strict=True, extra="forbid")
        except ValidationError:
            raise ToolError(str(TaskSearchInputError())) from None
        return data


def create_server(reader: McpQueryReader) -> FastMCP:
    """stdio와 상주 HTTP에 같은 조회 도구와 입력 계약을 제공한다."""
    read_only = ToolAnnotations(
        readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False
    )

    server = FastMCP(
        "maeng-mate",
        tools=[create_task_search_tool(reader, read_only)],
        host="127.0.0.1",
        stateless_http=True,
        json_response=True,
        streamable_http_path="/",
        log_level="WARNING",
    )

    @server.tool(annotations=read_only)
    async def get_my_system_status() -> dict[str, object]:
        """상주 앱의 구현·설정 상태를 읽는다. 수집과 알림은 시작하지 않는다."""
        try:
            return await reader.read_status()
        except StatusReadError:
            raise ToolError("로컬 업무 앱 조회에 실패했습니다.") from None

    @server.tool(annotations=read_only)
    async def get_my_today(
        date: str | None = None, event_classification: str | None = None
    ) -> dict[str, object]:
        """본인의 업무·QA를 모든 원본 상태로 읽고, 해당일 회의·휴가·기타 일정은 취소 건을 제외한다. event_classification을 지정하면 해당 사용자 분류의 일정만 반환한다. 날짜 생략 시 서울의 오늘이다. 휴가는 approvalState와 유형을 구별한다. 현재 저장된 자료의 stale·warnings·truncated를 확인한다."""
        try:
            if date is not None:
                parse_day(date)
            return await reader.read_today(date, event_classification)
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
    async def list_my_task_statuses() -> dict[str, object]:
        """본인이 조회할 수 있는 목록 소스별 sourceId·name, 사용자 카테고리 categoryId·categoryName과 원본 statuses를 읽는다. statusOptionsBasis는 스키마(schema), 본인 행에서 관찰한 값(observed), 자료 없음(unavailable)을 구별한다. tracksStatus와 hasUnspecifiedStatus, 수집 상태도 확인한다. 상태 검색 전에 소스별로 사용할 이름을 고른다."""
        try:
            return await reader.read_task_statuses()
        except StatusReadError:
            raise ToolError(
                "업무 상태 목록을 조회할 수 없습니다. 로컬 앱 연결과 수집 상태를 확인하세요."
            ) from None

    return server


def create_task_search_tool(reader: McpQueryReader, read_only: ToolAnnotations) -> Tool:
    """공통 파서가 HTTP와 같은 JSON 타입을 받도록 개인 검색 도구를 조립한다."""

    async def search_my_tasks(
        query: str | None = None,
        status: str | None = None,
        due_from: str | None = None,
        due_to: str | None = None,
        section: str = "all",
        limit: int = 50,
        offset: int = 0,
        source_id: str | None = None,
        statuses: list[str] | None = None,
        include_unset: bool = False,
        category_id: str | None = None,
    ) -> dict[str, object]:
        """본인의 목록 항목을 사용자 카테고리·제목·소스·상태·마감 범위로 찾는다. 기본은 모든 상태다. 상태 요청은 list_my_task_statuses로 이름을 확인하고 source_id와 원본 statuses를 함께 지정한다. statuses 중 하나라도 정확히 일치하면 포함하며 status는 한 이름용이다. include_unset은 미지정 행을 포함하고 단독 사용 시 미지정만 찾는다. 카테고리 검색에는 목록에서 확인한 categoryId를 category_id로 전달한다. section은 기존 호출용이다. 날짜는 서울 YYYY-MM-DD 양끝 포함이다. nextOffset으로 이어 읽고 stale·warnings를 확인한다."""
        try:
            search = TaskSearch.parse(
                {
                    "query": query,
                    "status": status,
                    "source_id": source_id,
                    "category_id": category_id,
                    "statuses": statuses,
                    "include_unset": include_unset,
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

    # 검색만 원본 JSON 타입을 강제하고 다른 도구의 SDK 입력 계약은 유지한다.
    search_tool = Tool.from_function(search_my_tasks, annotations=read_only)
    search_tool.fn_metadata = SearchArguments.model_validate(search_tool.fn_metadata.model_dump())
    return search_tool
