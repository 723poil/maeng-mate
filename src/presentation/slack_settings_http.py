"""관리자용 Slack 설정·인증 상태 API다."""

import asyncio
import json

from fastapi import APIRouter, HTTPException, Request

from src.application.credentials import CredentialError, validated_secret
from src.application.errors import SnapshotError
from src.application.slack_settings import SlackSettingsClient, SlackTestError, slack_state_payload
from src.presentation.request_body import read_limited_body


async def _body(request: Request) -> dict[str, object]:
    """작은 설정 본문만 받아 원문을 오류에 포함하지 않는다."""
    data = await read_limited_body(request, max_bytes=4096, too_large={"code": "invalid"})
    try:
        value: object = json.loads(data)
        if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
            raise ValueError
        return {key: item for key, item in value.items()}
    except (ValueError, UnicodeError):
        raise HTTPException(422, {"code": "invalid"}) from None


def _revision(value: object) -> int:
    """불명확한 버전을 저장·확인 요청에 사용하지 않는다."""
    if type(value) is not int or value < 0:
        raise CredentialError("invalid")
    return value


def create_slack_settings_router(service: SlackSettingsClient | None) -> APIRouter:
    """MCP와 분리된 앱 관리 경로에만 설정 동작을 제공한다."""
    router = APIRouter()

    @router.get("/v1/slack/settings")
    async def read() -> dict[str, object]:
        """토큰 원문은 반환하지 않고 조회로 설정 행을 만들지 않는다."""
        if service is None:
            raise HTTPException(503, {"code": "unavailable"})
        try:
            return slack_state_payload(await asyncio.to_thread(service.read))
        except CredentialError as error:
            raise HTTPException(503, {"code": error.code}) from None
        except SnapshotError:
            raise HTTPException(503, {"code": "unavailable"}) from None

    @router.post("/v1/slack/settings/{action}")
    async def manage(action: str, request: Request) -> dict[str, object]:
        """수신자 저장과 토큰 저장·확인·삭제를 독립 동작으로 실행한다."""
        if service is None:
            raise HTTPException(503, {"code": "unavailable"})
        raw = await _body(request)
        try:
            revision = _revision(raw.get("revision"))
            if action == "member" and set(raw) == {"revision", "userId"}:
                user = raw["userId"]
                if user is not None and not isinstance(user, str):
                    raise CredentialError("invalid")
                state = await asyncio.to_thread(service.save_member, revision, user)
            elif action == "token" and set(raw) == {"revision", "token"}:
                state = await asyncio.to_thread(
                    service.save_token, revision, validated_secret(raw["token"])
                )
            elif action == "app-token" and set(raw) == {"revision", "token"}:
                state = await asyncio.to_thread(
                    service.save_app_token, revision, validated_secret(raw["token"])
                )
            elif action == "app-delete" and set(raw) == {"revision"}:
                state = await asyncio.to_thread(service.delete_app_token, revision)
            elif action == "test" and set(raw) == {"revision", "credentialRevision"}:
                state = await asyncio.to_thread(
                    service.test_notification, revision, _revision(raw["credentialRevision"])
                )
            elif action == "check" and set(raw) == {"revision"}:
                state = await asyncio.to_thread(service.check, revision)
            elif action == "delete" and set(raw) == {"revision"}:
                state = await asyncio.to_thread(service.delete_token, revision)
            else:
                raise CredentialError("invalid")
            return slack_state_payload(state)
        except (CredentialError, SlackTestError) as error:
            raise HTTPException(
                409 if error.code == "conflict" else 422 if error.code == "invalid" else 503,
                {"code": error.code},
            ) from None
        except SnapshotError:
            raise HTTPException(503, {"code": "unavailable"}) from None

    return router
