"""Notion 토큰을 반환하지 않는 인증 관리 HTTP 경계다."""

import asyncio
import json
from typing import Literal

from fastapi import APIRouter, HTTPException, Request

from src.application.credentials import CredentialError, validated_secret
from src.application.notion_credentials import NotionCredentialsClient, credential_state_payload
from src.application.notion_people import people_payload
from src.presentation.credential_errors_http import credential_http_error
from src.presentation.request_body import read_limited_body


def create_notion_credentials_router(service: NotionCredentialsClient | None) -> APIRouter:
    """요청 본문으로만 토큰을 받고 예외 원문을 응답하지 않는다."""
    router = APIRouter()

    async def perform(
        request: Request | None, action: Literal["read", "check", "save", "delete"]
    ) -> dict[str, object]:
        """허용한 작은 본문과 버전으로만 비동기 관리 작업을 실행한다."""
        if service is None:
            raise HTTPException(503, {"code": "unavailable"})
        try:
            if action == "read":
                return credential_state_payload(await asyncio.to_thread(service.read))
            if request is None:
                raise CredentialError("invalid")
            body = await read_limited_body(request, max_bytes=4096, too_large={"code": "invalid"})
            raw: object = json.loads(body)
            if not isinstance(raw, dict) or set(raw) != (
                {"revision", "token"} if action == "save" else {"revision"}
            ):
                raise CredentialError("invalid")
            revision = raw["revision"]
            if type(revision) is not int or revision < 0:
                raise CredentialError("invalid")
            if action == "save":
                state = await asyncio.to_thread(
                    service.save, revision, validated_secret(raw["token"])
                )
            elif action == "delete":
                state = await asyncio.to_thread(service.delete, revision)
            else:
                state = await asyncio.to_thread(service.check, revision)
            return credential_state_payload(state)
        except (ValueError, UnicodeError):
            raise HTTPException(422, {"code": "invalid"}) from None
        except CredentialError as error:
            raise credential_http_error(error) from None

    @router.get("/v1/notion/credentials")
    async def read() -> dict[str, object]:
        """등록 상태만 반환하고 저장된 토큰을 반환하지 않는다."""
        return await perform(None, "read")

    @router.post("/v1/notion/credentials/authorize")
    async def authorize() -> dict[str, object]:
        """사용자가 설정에서 요청한 경우에만 키체인 승인 창을 허용한다."""
        if service is None:
            raise HTTPException(503, {"code": "unavailable"})
        try:
            return credential_state_payload(
                await asyncio.to_thread(service.read, allow_prompt=True)
            )
        except CredentialError as error:
            raise credential_http_error(error) from None

    @router.post("/v1/notion/credentials/check")
    async def check(request: Request) -> dict[str, object]:
        """현재 저장된 토큰의 연결만 확인한다."""
        return await perform(request, "check")

    @router.put("/v1/notion/credentials")
    async def save(request: Request) -> dict[str, object]:
        """검증에 성공한 새 토큰만 저장한다."""
        return await perform(request, "save")

    @router.delete("/v1/notion/credentials")
    async def delete(request: Request) -> dict[str, object]:
        """명시적 삭제와 환경값 복귀 방지를 함께 저장한다."""
        return await perform(request, "delete")

    @router.get("/v1/notion/people")
    async def people() -> dict[str, object]:
        """본인 선택에 쓸 토큰 주인과 사람 이름만 반환하고 토큰은 반환하지 않는다."""
        if service is None:
            raise HTTPException(503, {"code": "unavailable"})
        try:
            return people_payload(await asyncio.to_thread(service.people))
        except CredentialError as error:
            raise credential_http_error(error) from None

    return router
