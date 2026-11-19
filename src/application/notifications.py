"""로컬 알림 큐와 OS 요청을 분리하는 실행 계약이다."""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal, Protocol

type DeliveryResult = Literal["requested", "failed", "unknown", "cancelled"]


def parse_delivery_result(value: object) -> DeliveryResult:
    """채널의 최종 응답만 허용하고 잘못된 값은 오류 원문에 담지 않는다."""
    if value in ("requested", "failed", "unknown", "cancelled") and isinstance(value, str):
        return value
    raise ValueError("알림 결과를 확인하세요.")


@dataclass(frozen=True)
class NotificationChannel:
    """설정 화면과 전달 라우팅이 공유하는 설치된 채널 정보다."""

    key: str
    title: str


@dataclass(frozen=True)
class NotificationJob:
    """발송 직전 재검증할 단일 알림 실행권이다."""

    id: str
    token: str
    title: str
    body: str
    channel: str = "macos"
    url: str | None = None


class NotificationStore(Protocol):
    """알림 큐의 비동기 저장 계약이다."""

    async def claim(self) -> tuple[NotificationJob, ...]:
        """기한이 된 작업의 실행권을 획득한다."""
        ...

    async def authorize(self, identifier: str, token: str) -> bool:
        """현재 실행권을 읽기 검증한다."""
        ...

    async def ack(self, identifier: str, token: str, result: DeliveryResult) -> None:
        """OS 요청 결과를 확정한다."""
        ...


class NotificationQueue(Protocol):
    """조회 API와 구별한 명시적 claim·승인·결과 기록 계약이다."""

    def claim(self) -> tuple[NotificationJob, ...]:
        """기한이 된 알림의 실행권만 가져온다."""
        ...

    def authorize(self, job: NotificationJob) -> bool:
        """정정과 만료를 발송 직전에 다시 확인한다."""
        ...

    def ack(self, job: NotificationJob, result: DeliveryResult) -> None:
        """요청됨·실패·불명확·취소 결과를 보존한다."""
        ...


class NotificationSender(Protocol):
    """채널별 발송 방식을 교체한다."""

    def request(self, job: NotificationJob) -> DeliveryResult:
        """채널 요청 결과를 반환하며 실제 표시·수신을 보장하지 않는다."""
        ...


class NotificationRunner:
    """재시작 때도 불명확한 OS 요청을 무조건 재발송하지 않는다."""

    def __init__(self, queue: NotificationQueue, senders: Mapping[str, NotificationSender]) -> None:
        """로컬 큐와 설치된 채널 어댑터를 주입한다."""
        self._queue = queue
        self._senders = dict(senders)

    def run_once(self) -> bool:
        """발송 직전 승인을 확인하고 결과를 명시적으로 기록한다."""
        failed = False
        for job in self._queue.claim():
            if not self._queue.authorize(job):
                self._queue.ack(job, "cancelled")
                continue
            sender = self._senders.get(job.channel)
            try:
                result: DeliveryResult = sender.request(job) if sender is not None else "failed"
            except Exception:
                # 호출 중 예외는 외부 요청 여부를 단정할 수 없으므로 재전송하지 않는다.
                result = "unknown"
            self._queue.ack(job, result)
            failed = failed or result not in {"requested", "cancelled"}
        return failed
