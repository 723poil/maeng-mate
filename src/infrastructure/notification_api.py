"""메뉴바 알림 작업자가 사용하는 인증된 로컬 큐 어댑터다."""

import httpx

from src.application.errors import StatusReadError
from src.application.notifications import DeliveryResult, NotificationJob
from src.application.notion_links import notion_link
from src.infrastructure.api_decoding import boolean, json_object, text
from src.infrastructure.local_http import local_client, response_json


class LocalNotificationQueue:
    """노션 토큰·DB 경로 없이 루프백의 전달 API만 사용한다."""

    def __init__(
        self, address: str, token: str, *, transport: httpx.BaseTransport | None = None
    ) -> None:
        """공통 API 환경과 검증용 전송을 받는다."""
        self._address = address
        self._token = token
        self._transport = transport

    def _post(self, path: str, payload: dict[str, object] | None = None) -> dict[str, object]:
        """프록시·리다이렉트를 사용하지 않고 오류 원문을 숨긴다."""
        try:
            with local_client(
                self._address, self._token, timeout=3, transport=self._transport
            ) as client:
                response = client.post(
                    path,
                    json=payload,
                )
                response.raise_for_status()
                value: object = response_json(response, max_bytes=65536)
                return json_object(value)
        except (httpx.HTTPError, ValueError):
            raise StatusReadError("알림 연결을 확인하세요.") from None

    def claim(self) -> tuple[NotificationJob, ...]:
        """알림 원문은 승인된 채널 어댑터에만 전달한다."""
        try:
            rows = self._post("/v1/notifications/claim")["jobs"]
            if not isinstance(rows, list) or len(rows) > 1:
                raise ValueError
            result: list[NotificationJob] = []
            for row in rows:
                raw = json_object(row)
                result.append(
                    NotificationJob(
                        text(raw["id"]),
                        text(raw["token"]),
                        text(raw["title"]),
                        text(raw["body"]),
                        text(raw["channel"]),
                        notion_link(raw.get("url")),
                    )
                )
            return tuple(result)
        except (ValueError, KeyError):
            raise StatusReadError("알림 응답을 확인하지 못했습니다.") from None

    def authorize(self, job: NotificationJob) -> bool:
        """현재 실행권 승인을 요청하고 응답 타입을 확인한다."""
        try:
            return boolean(
                self._post("/v1/notifications/authorize", {"id": job.id, "token": job.token})[
                    "authorized"
                ]
            )
        except (ValueError, KeyError):
            raise StatusReadError("알림 승인을 확인하지 못했습니다.") from None

    def ack(self, job: NotificationJob, result: DeliveryResult) -> None:
        """불명확한 결과도 그대로 보존하고 무조건 재요청하지 않는다."""
        try:
            if (
                self._post(
                    "/v1/notifications/ack", {"id": job.id, "token": job.token, "result": result}
                )["acknowledged"]
                is not True
            ):
                raise ValueError
        except (ValueError, KeyError):
            raise StatusReadError("알림 결과를 저장하지 못했습니다.") from None
