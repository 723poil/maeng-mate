"""stdio 목록 MCP의 소비자 인증을 보존하는 루프백 어댑터다."""

import httpx

from src.application.errors import StatusReadError
from src.infrastructure.api_decoding import json_object
from src.infrastructure.local_http import response_json


class ListQueryApi:
    """기존 관리자 인증이나 개인 도구 경로를 재사용하지 않는다."""

    def __init__(self, client: httpx.AsyncClient) -> None:
        """목록 전용 토큰이 주입된 수명 관리 클라이언트를 받는다."""
        self.client = client

    async def catalog(self, digest: str | None) -> dict[str, object]:
        """stdio의 고정 소비자 인증으로 허용 목록을 읽는다."""
        return await self._read("/v1/mcp/lists/sources")

    async def search(self, digest: str | None, payload: object) -> dict[str, object]:
        """검증은 HTTP와 같은 경계에 맡기고 검색 원문을 URL에 넣지 않는다."""
        return await self._read("/v1/mcp/lists/search", payload)

    async def _read(self, path: str, payload: object | None = None) -> dict[str, object]:
        """실제 HTTP·JSON·크기 오류를 정상 빈 자료로 바꾸지 않는다."""
        try:
            response = (
                await self.client.get(path)
                if payload is None
                else await self.client.post(path, json=payload)
            )
            response.raise_for_status()
            return json_object(response_json(response, max_bytes=1024 * 1024))
        except (httpx.HTTPError, ValueError):
            raise StatusReadError("목록 자료를 읽지 못했습니다.") from None
