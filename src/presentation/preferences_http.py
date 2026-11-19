"""인증된 설정 조회·수정 요청을 저장 포트로 연결한다."""

import asyncio
import json

from fastapi import APIRouter, HTTPException, Request

from src.application.errors import SnapshotError
from src.application.preferences import (
    PreferencesConflictError,
    PreferencesStore,
    parse_preferences,
    preferences_payload,
)
from src.presentation.request_body import read_limited_body


def create_preferences_router(
    store: PreferencesStore | None, channels: tuple[str, ...]
) -> APIRouter:
    """서버가 조립한 채널만 설정으로 허용한다."""
    router = APIRouter()

    @router.get("/v1/preferences")
    async def read() -> dict[str, object]:
        """조회로 설정이나 알림 상태를 바꾸지 않는다."""
        if store is None:
            raise HTTPException(503, "설정 DB를 준비하세요.")
        try:
            return preferences_payload(await asyncio.to_thread(store.read))
        except SnapshotError:
            raise HTTPException(503, "설정을 읽지 못했습니다.") from None

    @router.put("/v1/preferences")
    async def save(request: Request) -> dict[str, object]:
        """작은 설정 본문과 확정 버전만 받아 비동기로 저장한다."""
        if store is None:
            raise HTTPException(503, "설정 DB를 준비하세요.")
        body = await read_limited_body(
            request, max_bytes=300000, too_large="설정 입력이 너무 큽니다."
        )
        try:
            raw: object = json.loads(body)
            current = await asyncio.to_thread(store.read)
            value = parse_preferences(raw, channels, current=current)
            return preferences_payload(await asyncio.to_thread(store.save, value))
        except (ValueError, UnicodeError):
            raise HTTPException(422, "설정 입력을 확인하세요.") from None
        except PreferencesConflictError:
            raise HTTPException(409, "설정이 변경되었습니다. 다시 불러와 주세요.") from None
        except SnapshotError:
            raise HTTPException(503, "설정 저장을 확인하지 못했습니다.") from None

    return router
