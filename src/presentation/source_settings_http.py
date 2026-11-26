"""관리자용 수집 소스 편집과 명시적 메타데이터 조회를 연결한다."""

import asyncio
import json
from uuid import UUID

from fastapi import APIRouter, HTTPException, Request

from src.application.category_settings_codec import category_documents
from src.application.errors import SnapshotError
from src.application.source_settings import (
    SourceSettingsClient,
    SourceSettingsError,
    SourceSettingsState,
    display_payload,
    draft_payload,
    parse_collection_draft,
    schema_payload,
    tables_payload,
)
from src.application.work_settings import WorkSettingsError
from src.presentation.request_body import read_limited_body


async def _body(request: Request, *, limit: int) -> object:
    """로컬 요청도 크기와 JSON 형식을 제한한다."""
    data = await read_limited_body(request, max_bytes=limit, too_large="입력 크기를 줄여 주세요.")
    try:
        value: object = json.loads(data)
        return value
    except (ValueError, UnicodeError):
        raise HTTPException(422, "수집 설정 입력을 확인하세요.") from None


def _payload(state: SourceSettingsState) -> dict[str, object]:
    """편집값과 재시작 표시만 반환한다."""
    return {
        "settings": draft_payload(state.settings),
        "restartRequired": state.restart_required,
        "activeDisplay": [display_payload(item) for item in state.active_display],
        "categories": category_documents(state.categories),
    }


def create_source_settings_router(service: SourceSettingsClient | None) -> APIRouter:
    """조회 전용 MCP에는 노출되지 않는 관리자 경계를 추가한다."""
    router = APIRouter(prefix="/v1/source-settings")

    @router.get("")
    async def read() -> dict[str, object]:
        """현재 JSON에서 저장된 소스를 읽는다."""
        if service is None:
            raise HTTPException(503, "수집 설정을 불러올 수 없습니다.")
        try:
            return _payload(await asyncio.to_thread(service.read))
        except (WorkSettingsError, SnapshotError):
            raise HTTPException(503, "수집 설정을 불러오지 못했습니다.") from None

    @router.put("")
    async def save(request: Request) -> dict[str, object]:
        """버전이 같고 매핑이 유효할 때만 저장한다."""
        if service is None:
            raise HTTPException(503, "수집 설정을 저장할 수 없습니다.")
        try:
            value = parse_collection_draft(await _body(request, limit=131072))
            return _payload(await asyncio.to_thread(service.save, value))
        except WorkSettingsError as error:
            if error.code == "conflict":
                raise HTTPException(409, "설정이 변경되었습니다. 다시 불러오세요.") from None
            raise HTTPException(
                422, "수집 소스가 중복되었거나 필수 매핑이 빠져 있습니다."
            ) from None
        except SourceSettingsError as error:
            raise HTTPException(422, str(error)) from None
        except SnapshotError:
            raise HTTPException(503, "수집 설정 저장을 확인하지 못했습니다.") from None

    @router.get("/categories/{category_id}/observed-choices")
    async def observed_choices(category_id: UUID) -> dict[str, tuple[str, ...]]:
        """관리자에게 지정한 카테고리의 선택값만 반환한다."""
        if service is None:
            raise HTTPException(503, "수집한 선택값을 조회할 수 없습니다.")
        try:
            return await asyncio.to_thread(service.observed_choices, str(category_id))
        except (WorkSettingsError, SnapshotError):
            raise HTTPException(503, "수집한 선택값을 읽지 못했습니다.") from None

    @router.get("/tables")
    async def tables(provider: str = "notion") -> dict[str, object]:
        """사용자가 요청한 경우에만 연결 가능한 원본 표 목록을 읽는다."""
        if service is None:
            raise HTTPException(503, "표 목록을 조회할 수 없습니다.")
        try:
            result = await asyncio.to_thread(service.tables, provider)
            return {"tables": tables_payload(result)}
        except SourceSettingsError as error:
            raise HTTPException(422, str(error)) from None

    @router.post("/columns")
    async def columns(request: Request) -> dict[str, object]:
        """사용자가 요청한 경우에만 외부 속성을 읽는다."""
        if service is None:
            raise HTTPException(503, "칼럼을 조회할 수 없습니다.")
        raw = await _body(request, limit=6144)
        if not isinstance(raw, dict) or set(raw) != {"provider", "reference", "rounds"}:
            raise HTTPException(422, "조회 입력을 확인하세요.")
        provider, reference, rounds = raw["provider"], raw["reference"], raw["rounds"]
        if (
            not isinstance(provider, str)
            or not isinstance(reference, str)
            or type(rounds) is not bool
        ):
            raise HTTPException(422, "조회 입력을 확인하세요.")
        try:
            result = await asyncio.to_thread(service.inspect, provider, reference, rounds=rounds)
            return {"schemas": [schema_payload(item) for item in result]}
        except SourceSettingsError as error:
            raise HTTPException(422, str(error)) from None

    return router
