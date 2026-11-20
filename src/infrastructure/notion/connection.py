"""Notion 인증과 현재 수집 소스의 최소 읽기 권한을 확인한다."""

import asyncio
import json
from collections.abc import Sequence
from urllib.parse import quote

import httpx

from src.application.credentials import CredentialError, CredentialSecret
from src.domain.models import SourceMapping
from src.infrastructure.notion.read_api import NOTION_API_URL, notion_headers


class NotionConnection:
    """고정 API 주소만 사용하고 응답 원문·개인 자료는 보관하지 않는다."""

    def __init__(
        self, sources: Sequence[SourceMapping], *, transport: httpx.AsyncBaseTransport | None = None
    ) -> None:
        """현재 소스와 검증용 전송 계층만 주입한다."""
        self._sources = tuple(sources)
        self._transport = transport

    def verify(self, token: CredentialSecret) -> None:
        """네트워크 전체 대기와 응답 크기를 제한하고 읽기 권한을 확인한다."""
        try:
            asyncio.run(self._verify(token))
        except (TimeoutError, httpx.TimeoutException):
            raise CredentialError("timeout") from None
        except (httpx.HTTPError, ValueError):
            raise CredentialError("unavailable") from None

    async def _verify(self, token: CredentialSecret) -> None:
        """마지막 응답이 늦어져도 전체 확인을9초 안에 중단한다."""
        async with (
            asyncio.timeout(9),
            httpx.AsyncClient(
                base_url=NOTION_API_URL,
                transport=self._transport,
                headers=notion_headers(token.value),
                trust_env=False,
                follow_redirects=False,
                timeout=3,
            ) as client,
        ):
            await self._request(client, "GET", "/users/me", "user")
            # 같은 표의 표시 설정이 여러 개여도 읽기 권한은 한 번만 확인한다.
            for origin in dict.fromkeys(source.origin for source in self._sources):
                await asyncio.sleep(0.4)
                await self._request(
                    client,
                    "POST",
                    f"/data_sources/{quote(origin, safe='')}/query",
                    "list",
                    {"page_size": 1},
                )

    async def _request(
        self,
        client: httpx.AsyncClient,
        method: str,
        path: str,
        kind: str,
        body: dict[str, object] | None = None,
    ) -> None:
        """오류 원문은 읽지 않고 응답도 상한 안에서만 받아 검사한다."""
        async with client.stream(method, path, json=body) as response:
            if response.status_code != 200:
                raise CredentialError(
                    "unauthorized"
                    if response.status_code == 401
                    else "rate_limited"
                    if response.status_code == 429
                    else "permission"
                    if response.status_code in (403, 404)
                    else "unavailable"
                )
            data = bytearray()
            async for chunk in response.aiter_bytes():
                if len(data) + len(chunk) > 1048576:
                    raise CredentialError("unavailable")
                data.extend(chunk)
            payload: object = json.loads(data)
            if not isinstance(payload, dict) or payload.get("object") != kind:
                raise CredentialError("unavailable")
            if kind == "user" and not isinstance(payload.get("id"), str):
                raise CredentialError("unavailable")
            if kind == "list" and not isinstance(payload.get("results"), list):
                raise CredentialError("unavailable")
