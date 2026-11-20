"""인증된 로컬 API를 읽는 외부 조회 어댑터다."""

import httpx

from src.application.errors import StatusReadError
from src.application.task_search import TaskSearch
from src.application.workday import workday_summary
from src.infrastructure.api_decoding import json_object
from src.infrastructure.day_response import day_view
from src.infrastructure.local_http import response_json


class LocalStatusReader:
    """조회 포트의 HTTP 구현이며 다른 서비스 자격정보는 보유하지 않는다."""

    def __init__(self, client: httpx.AsyncClient) -> None:
        """MCP 실행 수명에 연결된 로컬 조회 클라이언트를 받는다."""
        self._client = client

    async def read_status(self) -> dict[str, object]:
        """리다이렉트와 프록시 환경을 사용하지 않고 원본 HTTP 오류를 숨긴다."""
        return await self._read("/v1/status")

    async def read_today(
        self, requested_date: str | None = None, event_classification: str | None = None
    ) -> dict[str, object]:
        """사용자 ID나 소스 선택 없이 날짜만 로컬 API에 전달한다."""
        parameters = {"date": requested_date} if requested_date is not None else {}
        if event_classification is not None:
            parameters["event_classification"] = event_classification
        return await self._read("/v1/today", parameters)

    async def read_workday(self) -> dict[str, object]:
        """기존 근무 API 응답을 검증하고 조회에 필요한 사실만 반환한다."""
        try:
            return workday_summary(day_view(await self._read("/v1/day")))
        except (ValueError, KeyError, TypeError):
            raise StatusReadError("오늘 근무 기록 조회에 실패했습니다.") from None

    async def search_tasks(self, search: TaskSearch) -> dict[str, object]:
        """제목 검색어가 요청 URL 로그에 남지 않도록 본문으로 전달한다."""
        return await self._read("/v1/tasks/search", payload=search.payload())

    async def read_task_statuses(self) -> dict[str, object]:
        """검색 조건을 고르기 위해 인증된 개인 상태 목록만 읽는다."""
        return await self._read("/v1/tasks/statuses")

    async def _read(
        self,
        path: str,
        parameters: dict[str, str] | None = None,
        *,
        payload: dict[str, object] | None = None,
    ) -> dict[str, object]:
        """인증된 읽기 경로의 응답 크기와 JSON 객체를 확인한다."""
        try:
            response = (
                await self._client.get(path, params=parameters)
                if payload is None
                else await self._client.post(path, json=payload)
            )
            response.raise_for_status()
            response_payload = response_json(response, max_bytes=1024 * 1024)
            return json_object(response_payload)
        except (httpx.HTTPError, ValueError):
            raise StatusReadError("로컬 업무 앱 조회에 실패했습니다.") from None
