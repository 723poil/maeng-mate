"""인증된 일반·프로바이더 설정 편집의 HTTP 경계다."""

import asyncio
import json

from fastapi import APIRouter, HTTPException, Request

from src.application.errors import SnapshotError
from src.application.runtime_settings import RuntimeSettingsConflictError, parse_runtime_settings
from src.application.runtime_settings_service import RuntimeSettingsClient, runtime_state_payload
from src.presentation.request_body import read_limited_body


def create_runtime_settings_router(service: RuntimeSettingsClient | None) -> APIRouter:
    """설정·환경을 직접 읽지 않고 주입한 편집 유스케이스만 호출한다."""
    router = APIRouter()

    @router.get("/v1/runtime-settings")
    async def read() -> dict[str, object]:
        """기본 설정을 저장하지 않고 현재 편집 상태만 반환한다."""
        if service is None:
            raise HTTPException(503, "설정 DB를 준비하세요.")
        try:
            return runtime_state_payload(await asyncio.to_thread(service.read))
        except SnapshotError:
            raise HTTPException(503, "설정을 불러오지 못했습니다.") from None

    @router.put("/v1/runtime-settings")
    async def save(request: Request) -> dict[str, object]:
        """토큰·경로·미지원 키를 거부하고 버전 조건부 저장을 수행한다."""
        if service is None:
            raise HTTPException(503, "설정 DB를 준비하세요.")
        body = await read_limited_body(
            request, max_bytes=16384, too_large="설정 입력이 너무 큽니다."
        )
        try:
            current = await asyncio.to_thread(service.read)
            value = parse_runtime_settings(json.loads(body), current=current.settings)
            return runtime_state_payload(await asyncio.to_thread(service.save, value))
        except (ValueError, UnicodeError):
            raise HTTPException(422, "설정 입력을 확인하세요.") from None
        except RuntimeSettingsConflictError:
            raise HTTPException(409, "설정이 변경되었습니다. 다시 불러와 주세요.") from None
        except SnapshotError:
            raise HTTPException(503, "저장 여부를 확인하지 못했습니다.") from None

    return router
