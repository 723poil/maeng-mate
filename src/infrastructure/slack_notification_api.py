"""메뉴바 알림 작업자가 사용하는 Slack 전달용 로컬 API다."""

import httpx

from src.application.errors import StatusReadError
from src.application.notifications import DeliveryResult, NotificationJob, parse_delivery_result
from src.infrastructure.local_http import local_client, response_json


class SlackNotificationApi:
    """봇 토큰·수신자·본문 없이 실행권만 서버로 넘긴다."""

    def __init__(self, address: str, token: str) -> None:
        """앱 내부 루프백 주소와 관리 키만 주입한다."""
        self._address, self._token = address, token

    def request(self, job: NotificationJob) -> DeliveryResult:
        """외부 전송이 끝날 시간을 허용하되 HTTP 재시도는 하지 않는다."""
        if job.channel != "slack":
            return "cancelled"
        try:
            with local_client(self._address, self._token, timeout=20) as client:
                response = client.post(
                    "/v1/notifications/slack",
                    json={"id": job.id, "token": job.token},
                )
                response.raise_for_status()
                raw: object = response_json(response, max_bytes=4096)
                if not isinstance(raw, dict) or set(raw) != {"result"}:
                    raise ValueError
                return parse_delivery_result(raw["result"])
        except (httpx.HTTPError, ValueError):
            raise StatusReadError("Slack 알림 결과를 확인하지 못했습니다.") from None
