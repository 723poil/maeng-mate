"""Notion 고정 경로의 읽기 요청과 커서·하위 표 탐색을 담당한다."""

import asyncio
import json
import re
from collections.abc import AsyncIterator
from copy import deepcopy
from urllib.parse import quote

import httpx

from src.infrastructure.notion.errors import SourceError
from src.infrastructure.notion.values import array, record, text

NOTION_API_URL = "https://api.notion.com/v1"


def notion_headers(token: str) -> dict[str, str]:
    """수집·설정·권한 확인에 동일한 Notion API 버전을 사용한다."""
    return {"Authorization": f"Bearer {token}", "Notion-Version": "2026-03-11"}


class NotionReadApi:
    """하나의 클라이언트에서 요청 간격과 페이지 순서를 유지한다."""

    def __init__(self, token: str, client: httpx.AsyncClient, *, interval: float = 0.4) -> None:
        """HTTP와 요청 간격을 주입하고 비밀값은 객체 표현에 노출하지 않는다."""
        self._token = token
        self._client = client
        self._interval = interval
        self._next_request = 0.0

    async def request(self, path: str, body: dict[str, object] | None) -> dict[str, object]:
        """요청 간격을 지키고 재시도는 영속 예약 경계에 맡긴다."""
        try:
            now = asyncio.get_running_loop().time()
            slot = max(now, self._next_request)
            self._next_request = slot + self._interval
            await asyncio.sleep(slot - now)
            response = await self._client.request(
                "GET" if body is None else "POST",
                NOTION_API_URL + path,
                json=body,
                timeout=10,
                follow_redirects=False,
                headers=notion_headers(self._token),
            )
            if response.status_code != 200:
                codes = {
                    401: "unauthorized",
                    403: "permission_denied",
                    404: "not_found",
                    429: "rate_limited",
                    529: "rate_limited",
                    500: "unavailable",
                    502: "unavailable",
                    503: "unavailable",
                    504: "unavailable",
                }
                code = codes.get(response.status_code, "request_invalid")
                # 공개 API 차단은 일시 요청 제한과 달리 설정 확인이 필요하다.
                if response.status_code == 429:
                    try:
                        error_body: object = response.json()
                    except ValueError:
                        error_body = None
                    if (
                        isinstance(error_body, dict)
                        and error_body.get("code") == "public_api_request_blocked"
                    ):
                        code = "request_invalid"
                header = response.headers.get("Retry-After", "").strip()
                retry_after = float(header) if re.fullmatch(r"\d+(?:\.\d+)?", header) else None
                raise SourceError(code, retry_after=retry_after)
            payload: object = response.json()
            return record(payload)
        except SourceError:
            raise
        except httpx.TimeoutException:
            raise SourceError("timeout") from None
        except httpx.HTTPError:
            raise SourceError("unavailable") from None
        except ValueError:
            raise SourceError("invalid_response") from None

    async def items(self, path: str, *, query_source: bool = False) -> AsyncIterator[object]:
        """커서 순환을 감지하고 next_url 대신 고정 원천 경로만 사용한다."""
        cursor: str | None = None
        seen: set[str] = set()
        for _ in range(1000):
            body: dict[str, object] = {"page_size": 100}
            if cursor is not None:
                body["start_cursor"] = cursor
            if query_source:
                payload = await self.request(path, body)
            else:
                query = httpx.QueryParams({key: str(value) for key, value in body.items()})
                payload = await self.request(f"{path}?{query}", None)
            if payload.get("object") != "list" or type(payload.get("has_more")) is not bool:
                raise SourceError("invalid_response")
            # 이 요청이 소유한 배열을 뒤에서 꺼내면 원래 순서를 지키면서 처리한 행의
            # 참조를 놓을 수 있다. 행에 추가 조회한 상세 값도 다음 페이지까지 남지 않는다.
            results = array(payload.get("results"))
            results.reverse()
            while results:
                yield results.pop()
            if not payload["has_more"]:
                return
            cursor = text(payload.get("next_cursor"))
            if not cursor or cursor in seen:
                raise SourceError("pagination_invalid")
            seen.add(cursor)
        raise SourceError("pagination_invalid")

    async def data_schema(self, identifier: str) -> dict[str, object]:
        """고정 API 경로에서 데이터 소스 응답만 받는다."""
        value = await self.request(f"/data_sources/{quote(identifier, safe='')}", None)
        if value.get("object") != "data_source":
            raise SourceError("invalid_response")
        return value

    async def child_source_ids(self, page_id: str) -> AsyncIterator[str]:
        """한 차시 아래에 연결된 원본 표들을 수집과 설정에서 같은 범위로 읽는다."""
        async for database_id in self.child_databases(page_id):
            database = await self.request(f"/databases/{quote(database_id, safe='')}", None)
            for raw in array(database.get("data_sources")):
                yield text(record(raw).get("id"))

    async def child_databases(self, page_id: str, *, depth: int = 0) -> AsyncIterator[str]:
        """차시 페이지의 중첩 블록도 읽되 제한을 넘으면 부분 성공으로 처리하지 않는다."""
        if depth > 8:
            raise SourceError("invalid_response")
        async for raw in self.items(f"/blocks/{quote(page_id, safe='')}/children"):
            block = record(raw)
            identifier = text(block.get("id"))
            if block.get("type") == "child_database":
                yield identifier
            elif block.get("has_children") is True:
                async for child in self.child_databases(identifier, depth=depth + 1):
                    yield child

    def source_pages(self, identifier: str) -> AsyncIterator[object]:
        """데이터 소스의 모든 행을 POST 본문의 커서 순서로 읽는다."""
        return self.items(f"/data_sources/{quote(identifier, safe='')}/query", query_source=True)


class SharedNotionReadApi(NotionReadApi):
    """같은 원본에 대한 수집 한 회에서 동일 경로와 커서의 응답을 재사용한다."""

    def __init__(self, upstream: NotionReadApi) -> None:
        """원본 HTTP 클라이언트의 속도 제한을 공유하고 이번 수집의 결과만 보관한다."""
        self._upstream = upstream
        self._responses: dict[tuple[str, str], dict[str, object]] = {}
        self._failures: dict[tuple[str, str], tuple[str, float | None]] = {}

    async def request(self, path: str, body: dict[str, object] | None) -> dict[str, object]:
        """행 소비나 상세 속성 보완이 다른 매핑이 읽을 원본 응답을 바꾸지 않게 한다."""
        key = (path, json.dumps(body, sort_keys=True))
        if key in self._failures:
            code, delay = self._failures[key]
            raise SourceError(code, retry_after=delay)
        if key not in self._responses:
            try:
                self._responses[key] = await self._upstream.request(path, body)
            except SourceError as error:
                # 실패한 요청도 이번 실행에서 반복하지 않고 다음 예약에서 다시 시도한다.
                self._failures[key] = (error.code, error.retry_after)
                raise
        return deepcopy(self._responses[key])
