"""앱 관리 키로만 사용하는 Notion 페이지 링크 편집 경계다."""

import asyncio
import json

from fastapi import APIRouter, HTTPException, Request

from src.application.errors import SnapshotError
from src.application.notion_page_links import (
    NotionPageLinksClient,
    PageLinksError,
    page_links_payload,
    page_url,
)
from src.presentation.request_body import read_limited_body


def create_page_links_router(service: NotionPageLinksClient | None) -> APIRouter:
    """현재 수집 연결의 주소만 관리 포트에 전달한다."""
    router = APIRouter()

    @router.get("/v1/notion/page-links")
    async def read() -> dict[str, object]:
        """표시용 주소만 읽고 기본값을 저장하지 않는다."""
        if service is None:
            raise HTTPException(503, "페이지 링크 설정을 준비하세요.")
        try:
            return page_links_payload(await asyncio.to_thread(service.read))
        except SnapshotError:
            raise HTTPException(503, "페이지 링크를 불러오지 못했습니다.") from None

    @router.put("/v1/notion/page-links")
    async def save(request: Request) -> dict[str, object]:
        """기존 소스 주소 전체를 검증한 뒤 한 번에 저장한다."""
        if service is None:
            raise HTTPException(503, "페이지 링크 설정을 준비하세요.")
        body = await read_limited_body(
            request, max_bytes=524288, too_large="페이지 링크 입력이 너무 큽니다."
        )
        try:
            raw: object = json.loads(body)
            if not isinstance(raw, dict) or set(raw) != {"revision", "links"}:
                raise PageLinksError("invalid")
            revision, rows = raw["revision"], raw["links"]
            if type(revision) is not int or revision < 0 or not isinstance(rows, list):
                raise PageLinksError("invalid")
            links: dict[str, str | None] = {}
            for row in rows:
                if not isinstance(row, dict) or set(row) != {"sourceId", "url"}:
                    raise PageLinksError("invalid")
                identifier = row["sourceId"]
                if not isinstance(identifier, str) or not identifier or identifier in links:
                    raise PageLinksError("invalid")
                links[identifier] = page_url(row["url"])
            return page_links_payload(await asyncio.to_thread(service.save, revision, links))
        except (ValueError, UnicodeError):
            raise HTTPException(422, "Notion 페이지 주소를 확인하세요.") from None
        except PageLinksError as error:
            raise HTTPException(
                409 if error.code == "conflict" else 422,
                "링크 목록이 바뀌었습니다. 다시 불러오세요."
                if error.code == "conflict"
                else "Notion 페이지 주소를 확인하세요.",
            ) from None
        except SnapshotError:
            raise HTTPException(503, "링크 저장 결과를 확인하지 못했습니다.") from None

    return router
