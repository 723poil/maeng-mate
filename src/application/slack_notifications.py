"""저장된 알림만 Slack DM으로 전달하고 결과를 영속 확정한다."""

import asyncio
from typing import Protocol

from src.application.credentials import CredentialError, CredentialSecret, CredentialStore
from src.application.errors import SnapshotError
from src.application.notifications import DeliveryResult, NotificationJob
from src.application.runtime_settings import RuntimeSettingsStore
from src.application.slack_settings import SlackTestError


class SlackMessageGateway(Protocol):
    """일반 DM 전송 구현을 업무 알림과 분리한다."""

    def send(
        self, token: CredentialSecret, user_id: str, text: str, *, url: str | None = None
    ) -> None:
        """한 번 요청하고 불명확한 결과를 자동 재시도하지 않는다."""
        ...


class SlackDeliveryStore(Protocol):
    """영속 실행권과 현재 사실의 검증·결과 확정 계약이다."""

    async def reserve_slack(self, identifier: str, token: str) -> NotificationJob | None:
        """유효한 Slack 실행권의 발송 시작을 단 한 번 기록한다."""
        ...

    async def authorize(self, identifier: str, token: str) -> bool:
        """실제 외부 발송 직전에도 정정·구독·만료를 재검사한다."""
        ...

    async def complete_slack(self, identifier: str, token: str, result: DeliveryResult) -> None:
        """서버가 확정한 결과를 늦은 클라이언트 응답이 바꾸지 못하게 한다."""
        ...


class SlackNotificationDelivery:
    """클라이언트가 보낸 본문·수신자를 사용하지 않는 단일 발송 유스케이스다."""

    def __init__(
        self,
        store: SlackDeliveryStore,
        settings: RuntimeSettingsStore,
        credentials: CredentialStore,
        sender: SlackMessageGateway,
    ) -> None:
        """보안·일반 저장과 실제 메시지 전송 포트를 주입한다."""
        self._store, self._settings, self._credentials, self._sender = (
            store,
            settings,
            credentials,
            sender,
        )

    async def deliver(self, identifier: str, token: str) -> DeliveryResult:
        """발송 결과는 HTTP 응답 전에 기록하고 재호출은 실행하지 않는다."""
        job = await self._store.reserve_slack(identifier, token)
        if job is None:
            return "cancelled"
        result: DeliveryResult
        try:
            settings, credential = await asyncio.gather(
                asyncio.to_thread(self._settings.read), asyncio.to_thread(self._credentials.read)
            )
            if (
                settings is None
                or settings.slack.user_id is None
                or credential is None
                or credential.token is None
            ):
                result = "failed"
            elif not await self._store.authorize(identifier, token):
                result = "cancelled"
            else:
                await asyncio.to_thread(
                    self._sender.send,
                    credential.token,
                    settings.slack.user_id,
                    f"{job.title}\n{job.body}",
                    url=job.url,
                )
                result = "requested"
        except (CredentialError, SnapshotError):
            result = "failed"
        except SlackTestError as error:
            result = "unknown" if error.code == "delivery_unknown" else "failed"
        except Exception:
            # 전송 중 예외는 성공·실패를 추측하지 않아 중복 발송을 막는다.
            result = "unknown"
        await self._store.complete_slack(identifier, token, result)
        return result
