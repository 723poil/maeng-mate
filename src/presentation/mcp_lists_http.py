"""목록 전용 조회와 관리자 전용 발급을 같은 권한 포트에 연결한다."""

import asyncio
import json

from fastapi import APIRouter, HTTPException, Request

from src.application.errors import QueryReadError, SnapshotError
from src.application.mcp_lists import ListAccessError, ListQueries
from src.application.task_search import TaskSearchInputError
from src.presentation.request_body import read_limited_body


def create_list_router(service: ListQueries | None) -> APIRouter:
    """관리 인증과 목록 전용 인증은 상위 미들웨어가 분리한다."""
    router = APIRouter()

    def configured() -> ListQueries:
        """연결되지 않은 저장소를 빈 성공으로 숨기지 않는다."""
        if service is None:
            raise HTTPException(503, "목록 조회를 준비하세요.")
        return service

    async def body(request: Request) -> object:
        """검색과 발급 입력을 제한하고 외부 JSON을 객체로 받는다."""
        raw = await read_limited_body(request, max_bytes=16384, too_large="입력이 너무 큽니다.")
        try:
            result: object = json.loads(raw)
            return result
        except (ValueError, UnicodeError):
            raise HTTPException(422, "입력을 확인하세요.") from None

    @router.get("/v1/mcp/list-access/catalog")
    async def available() -> dict[str, object]:
        """관리자가 발급 시 선택할 목록 소스와 칼럼을 읽는다."""
        current = configured()
        return {"sources": [current.describe(source) for source in current.available().values()]}

    @router.get("/v1/mcp/list-access/tokens")
    async def tokens() -> dict[str, object]:
        """발급 원문 없이 현재 전용 접근 목록만 읽는다."""
        try:
            return {"tokens": await asyncio.to_thread(configured().access.tokens)}
        except (SnapshotError, ValueError):
            raise HTTPException(503, "목록 접근 상태를 읽지 못했습니다.") from None

    @router.post("/v1/mcp/list-access/tokens")
    async def issue(request: Request) -> dict[str, object]:
        """소스 전체 행과 칼럼의 명시 동의로 새 전용 토큰을 발급한다."""
        raw = await body(request)
        try:
            return await asyncio.to_thread(configured().issue, raw)
        except TaskSearchInputError:
            raise HTTPException(422, "소스·칼럼·전체 행 동의·이름·기간을 확인하세요.") from None
        except SnapshotError:
            raise HTTPException(
                503, "발급 결과를 확인하지 못했습니다. 목록을 확인하세요."
            ) from None

    @router.post("/v1/mcp/list-access/tokens/{identifier}/revoke")
    async def revoke(identifier: str) -> dict[str, object]:
        """선택한 목록 전용 토큰만 폐기한다."""
        if len(identifier) != 32 or any(c not in "0123456789abcdef" for c in identifier):
            raise HTTPException(422, "토큰을 확인하세요.")
        try:
            await asyncio.to_thread(configured().access.revoke, identifier)
            return {"revoked": True}
        except ListAccessError:
            raise HTTPException(404, "토큰을 찾지 못했습니다.") from None
        except SnapshotError:
            raise HTTPException(503, "폐기 결과를 확인하지 못했습니다.") from None

    @router.get("/v1/mcp/lists/sources")
    async def sources(request: Request) -> dict[str, object]:
        """목록 전용 호출자의 허용된 소스만 읽는다."""
        digest: object = request.scope.get("maeng.list_digest")
        try:
            return await configured().catalog(digest if isinstance(digest, str) else None)
        except ListAccessError:
            raise HTTPException(403, "목록 조회 권한을 확인하세요.") from None
        except SnapshotError:
            raise HTTPException(503, "목록 자료를 읽지 못했습니다.") from None

    @router.post("/v1/mcp/lists/search")
    async def search(request: Request) -> dict[str, object]:
        """검증한 조건과 호출자 인증을 동일한 목록 유스케이스에 전달한다."""
        digest: object = request.scope.get("maeng.list_digest")
        raw = await body(request)
        try:
            return await configured().search(digest if isinstance(digest, str) else None, raw)
        except ListAccessError:
            raise HTTPException(403, "목록 조회 권한을 확인하세요.") from None
        except TaskSearchInputError:
            raise HTTPException(422, "허용된 칼럼과 검색 조건을 확인하세요.") from None
        except (QueryReadError, SnapshotError):
            raise HTTPException(503, "목록 자료를 읽지 못했습니다.") from None

    return router
