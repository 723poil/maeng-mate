"""앱 관리 키로만 접근하는 MCP 설정·토큰 발급 HTTP다."""

import asyncio
import json

from fastapi import APIRouter, HTTPException, Request

from src.application.errors import SnapshotError
from src.application.mcp_access import McpAccessError, McpSettingsClient
from src.application.mcp_settings import mcp_state_payload
from src.presentation.request_body import read_limited_body


def create_mcp_settings_router(service: McpSettingsClient | None) -> APIRouter:
    """인증·권한 검사는 상위 앱에서 수행하고 관리 포트만 호출한다."""
    router = APIRouter()

    async def body(request: Request, keys: set[str]) -> dict[str, object]:
        """발급 요청과 설정 입력을 작은 JSON 객체로 제한한다."""
        data = await read_limited_body(request, max_bytes=4096, too_large="입력이 너무 큽니다.")
        try:
            raw: object = json.loads(data)
            if not isinstance(raw, dict) or set(raw) != keys:
                raise ValueError
            return {key: raw[key] for key in keys}
        except (ValueError, UnicodeError):
            raise HTTPException(422, "입력을 확인하세요.") from None

    @router.get("/v1/mcp/settings")
    async def read() -> dict[str, object]:
        """주소·준비·발급 메타데이터를 조회한다."""
        if service is None:
            raise HTTPException(503, "MCP 설정을 준비하세요.")
        try:
            return mcp_state_payload(await asyncio.to_thread(service.read))
        except SnapshotError:
            raise HTTPException(503, "MCP 상태를 읽지 못했습니다.") from None

    @router.put("/v1/mcp/settings")
    async def update(request: Request) -> dict[str, object]:
        """사용 선택은 읽은 설정 버전에서만 변경한다."""
        if service is None:
            raise HTTPException(503, "MCP 설정을 준비하세요.")
        raw = await body(request, {"revision", "enabled"})
        revision, enabled = raw["revision"], raw["enabled"]
        if type(revision) is not int or revision < 0 or type(enabled) is not bool:
            raise HTTPException(422, "입력을 확인하세요.")
        try:
            return mcp_state_payload(
                await asyncio.to_thread(service.set_enabled, revision, enabled)
            )
        except McpAccessError:
            raise HTTPException(409, "설정이 바뀌었습니다. 다시 불러오세요.") from None
        except SnapshotError:
            raise HTTPException(503, "설정 저장을 확인하지 못했습니다.") from None

    @router.post("/v1/mcp/tokens")
    async def issue(request: Request) -> dict[str, object]:
        """원문은 이 발급 응답 한 번에서만 반환한다."""
        if service is None:
            raise HTTPException(503, "MCP 설정을 준비하세요.")
        raw = await body(request, {"name", "expiresInDays"})
        name, days = raw["name"], raw["expiresInDays"]
        if not isinstance(name, str) or (days is not None and type(days) is not int):
            raise HTTPException(422, "입력을 확인하세요.")
        try:
            result = await asyncio.to_thread(service.issue, name, days)
            return {
                "state": mcp_state_payload(result.state),
                "token": result.token.value,
                "id": result.id,
            }
        except McpAccessError:
            raise HTTPException(422, "이름과 만료 기간을 확인하세요.") from None
        except SnapshotError:
            raise HTTPException(
                503, "발급 결과를 확인하지 못했습니다. 목록을 다시 읽으세요."
            ) from None

    @router.post("/v1/mcp/tokens/{identifier}/revoke")
    async def revoke(identifier: str) -> dict[str, object]:
        """원문을 다시 조회하지 않고 접근을 폐기한다."""
        if service is None:
            raise HTTPException(503, "MCP 설정을 준비하세요.")
        if len(identifier) != 32 or any(c not in "0123456789abcdef" for c in identifier):
            raise HTTPException(422, "토큰을 확인하세요.")
        try:
            return mcp_state_payload(await asyncio.to_thread(service.revoke, identifier))
        except McpAccessError:
            raise HTTPException(404, "토큰을 찾지 못했습니다.") from None
        except SnapshotError:
            raise HTTPException(503, "폐기 결과를 확인하지 못했습니다.") from None

    return router
