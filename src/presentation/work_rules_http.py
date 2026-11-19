"""관리자용 근무 규칙 설정 API다."""

import asyncio
import json

from fastapi import APIRouter, HTTPException, Request

from src.application.work_rules_settings import (
    WorkRulesClient,
    WorkRulesConflictError,
    WorkRulesState,
    document_payload,
    parse_document,
)
from src.presentation.request_body import read_limited_body


def _payload(state: WorkRulesState) -> dict[str, object]:
    """저장값과 적용 대기 상태만 반환한다."""
    return {
        "settings": document_payload(state.settings),
        "restartRequired": state.restart_required,
    }


def create_work_rules_router(service: WorkRulesClient | None) -> APIRouter:
    """경로 선택을 받지 않고 주입된 규칙 파일만 편집한다."""
    router = APIRouter(prefix="/v1/work-rules")

    @router.get("")
    async def read() -> dict[str, object]:
        """현재 편집값을 조회한다."""
        if service is None:
            raise HTTPException(503, "근무 규칙을 불러올 수 없습니다.")
        try:
            return _payload(await asyncio.to_thread(service.read))
        except (OSError, ValueError):
            raise HTTPException(503, "근무 규칙 파일을 확인하세요.") from None

    @router.put("")
    async def save(request: Request) -> dict[str, object]:
        """검증한 JSON만 이전 버전과 대조해 저장한다."""
        if service is None:
            raise HTTPException(503, "근무 규칙을 저장할 수 없습니다.")
        body = await read_limited_body(
            request, max_bytes=16384, too_large="근무 규칙이 너무 큽니다."
        )
        try:
            document = parse_document(json.loads(body))
        except (UnicodeError, ValueError):
            raise HTTPException(422, "근무 규칙 입력을 확인하세요.") from None
        try:
            return _payload(await asyncio.to_thread(service.save, document))
        except WorkRulesConflictError:
            raise HTTPException(409, "규칙이 변경되었습니다. 다시 불러오세요.") from None
        except (OSError, ValueError):
            raise HTTPException(503, "근무 규칙 파일을 확인하세요.") from None

    return router
