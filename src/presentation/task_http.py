"""검색 원문을 URL에 남기지 않는 읽기 전용 HTTP 경계다."""

import json

from fastapi import APIRouter, HTTPException, Request

from src.application.errors import QueryReadError
from src.application.task_search import TaskSearch, TaskSearchInputError, TaskSearchQuery
from src.presentation.request_body import read_limited_body


def create_task_router(query: TaskSearchQuery | None) -> APIRouter:
    """개인 범위가 고정된 검색 유스케이스만 주입받는다."""
    router = APIRouter()

    @router.post("/v1/tasks/search")
    async def search_tasks(request: Request) -> dict[str, object]:
        """크기를 제한하고 오류에 검색 원문을 포함하지 않는다."""
        body = await read_limited_body(
            request, max_bytes=4096, too_large="검색 입력 크기를 줄여 주세요."
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
