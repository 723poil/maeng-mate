"""검색 원문을 URL에 남기지 않는 읽기 전용 HTTP 경계다."""

import json

from fastapi import APIRouter, HTTPException, Request

from src.application.errors import QueryReadError
from src.application.task_search import (
    TaskSearch,
    TaskSearchInputError,
    TaskSearchQuery,
    TaskStatusesQuery,
)
from src.presentation.request_body import read_limited_body


def create_task_router(
    query: TaskSearchQuery | None, query_statuses: TaskStatusesQuery | None
) -> APIRouter:
    """본인 업무 검색과 소스별 상태 조회 유스케이스를 주입받는다."""
    router = APIRouter()

    @router.get("/v1/tasks/statuses")
    async def list_task_statuses() -> dict[str, object]:
        """연결된 업무·QA 소스의 상태 목록을 저장된 자료에서 읽는다."""
        if query_statuses is None:
            raise HTTPException(503, detail="업무 상태 목록 조회 설정이 필요합니다.")
        try:
            return await query_statuses()
        except QueryReadError:
            raise HTTPException(503, detail="저장된 업무 상태 목록을 조회할 수 없습니다.") from None

    @router.post("/v1/tasks/search")
    async def search_tasks(request: Request) -> dict[str, object]:
        """크기를 제한하고 오류에 검색 원문을 포함하지 않는다."""
        body = await read_limited_body(
            request, max_bytes=64 * 1024, too_large="검색 입력 크기를 줄여 주세요."
        )
        try:
            payload: object = json.loads(body)
            search = TaskSearch.parse(payload)
            if query is None:
                raise HTTPException(503, detail="개인 업무 검색 설정이 필요합니다.")
            return await query(search)
        except QueryReadError:
            raise HTTPException(503, detail="저장된 업무를 검색할 수 없습니다.") from None
        except (ValueError, UnicodeError):
            raise HTTPException(422, detail=str(TaskSearchInputError())) from None

    return router
