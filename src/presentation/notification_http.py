"""알림 실행권 변경은 인증된 명시적 POST로만 처리한다."""

import json
from dataclasses import asdict

from fastapi import APIRouter, HTTPException, Request

from src.application.errors import SnapshotError
from src.application.notifications import NotificationStore, parse_delivery_result
from src.application.slack_notifications import SlackNotificationDelivery
from src.presentation.request_body import read_limited_body


async def command(request: Request, *, acknowledgement: bool = False) -> tuple[str, str, str]:
    """알림 원문이나 임의 명령을 입력으로 받지 않는다."""
    body = await read_limited_body(request, max_bytes=1024, too_large="입력 크기를 줄여 주세요.")
    try:
        raw: object = json.loads(body)
        keys = {"id", "token", "result"} if acknowledgement else {"id", "token"}
        if not isinstance(raw, dict) or set(raw) != keys:
            raise ValueError
        identifier, token = raw["id"], raw["token"]
        result = raw["result"] if acknowledgement else ""
        if (
            not isinstance(identifier, str)
            or not 1 <= len(identifier) <= 128
            or not isinstance(token, str)
            or len(token) > 128
            or not isinstance(result, str)
        ):
            raise ValueError
        return identifier, token, result
    except (ValueError, UnicodeError):
        raise HTTPException(422, detail="알림 입력을 확인하세요.") from None


def create_notification_router(
    store: NotificationStore | None, slack: SlackNotificationDelivery | None = None
) -> APIRouter:
    """MCP의 읽기 도구와 별개로 GUI의 전달 요청만 연결한다."""
    router = APIRouter()

    @router.post("/v1/notifications/claim")
    async def claim() -> dict[str, object]:
        """최대 한 작업만 전달해 실행권 대기 시간을 제한한다."""
        if store is None:
            raise HTTPException(503, detail="알림 DB 설정이 필요합니다.")
        try:
            return {"jobs": [asdict(job) for job in await store.claim()]}
        except SnapshotError:
            raise HTTPException(503, detail="알림 DB를 준비하거나 연결을 확인하세요.") from None

    @router.post("/v1/notifications/authorize")
    async def authorize(request: Request) -> dict[str, object]:
        """정정된 오래된 예약은 OS 호출 직전에 거부한다."""
        if store is None:
            raise HTTPException(503, detail="알림 DB 설정이 필요합니다.")
        identifier, token, _ = await command(request)
        try:
            return {"authorized": await store.authorize(identifier, token)}
        except SnapshotError:
            raise HTTPException(503, detail="알림 상태를 확인하지 못했습니다.") from None

    @router.post("/v1/notifications/slack")
    async def deliver_slack(request: Request) -> dict[str, object]:
        """본문·수신자 대신 영속 실행권만 받아 Slack 전달을 요청한다."""
        if slack is None:
            raise HTTPException(503, detail="Slack 알림 설정을 확인하세요.")
        identifier, token, _ = await command(request)
        try:
            return {"result": await slack.deliver(identifier, token)}
        except SnapshotError:
            raise HTTPException(503, detail="Slack 알림 결과를 확인하지 못했습니다.") from None

    @router.post("/v1/notifications/ack")
    async def ack(request: Request) -> dict[str, object]:
        """허용한 결과만 저장하고 잘못된 원문은 반환하지 않는다."""
        if store is None:
            raise HTTPException(503, detail="알림 DB 설정이 필요합니다.")
        identifier, token, result = await command(request, acknowledgement=True)
        try:
            await store.ack(identifier, token, parse_delivery_result(result))
            return {"acknowledged": True}
        except SnapshotError:
            raise HTTPException(503, detail="알림 결과를 저장하지 못했습니다.") from None
        except ValueError:
            raise HTTPException(422, detail="알림 결과를 확인하세요.") from None

    return router
