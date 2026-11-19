"""수집 주기 변경의 인증 후 입력 경계다."""

import json

from fastapi import APIRouter, HTTPException, Request

from src.application.errors import SnapshotError
from src.application.periodic import IntervalUpdate
from src.presentation.request_body import read_limited_body


def create_collection_router(update: IntervalUpdate | None) -> APIRouter:
    """상주 실행기가 주입된 경우에만 예약 변경을 허용한다."""
    router = APIRouter()

    @router.put("/v1/collection/interval")
    async def change_interval(request: Request) -> dict[str, object]:
        """크기·키·정수 범위를 검사하고 원문을 오류 응답에 넣지 않는다."""
        if update is None:
            raise HTTPException(503, detail="정기 수집 앱을 실행하세요.")
        body = await read_limited_body(
            request, max_bytes=1024, too_large="입력 크기를 줄여 주세요."
        )
        try:
            payload: object = json.loads(body)
            if not isinstance(payload, dict) or set(payload) != {"intervalSeconds"}:
                raise ValueError
            seconds = payload["intervalSeconds"]
            if type(seconds) is not int or not 60 <= seconds <= 86400:
                raise ValueError
            return await update(seconds)
        except (ValueError, UnicodeError):
            raise HTTPException(422, detail="수집 주기는 60~86400초의 정수여야 합니다.") from None
        except SnapshotError:
            raise HTTPException(503, detail="수집 주기를 저장하지 못했습니다.") from None

    return router
