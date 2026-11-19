"""기존 알림 전달 계약을 전용 macOS 앱으로 연결한다."""

from collections.abc import Callable

from src.application.notifications import DeliveryResult, NotificationJob, parse_delivery_result
from src.domain.preferences import Preferences
from src.infrastructure.macos_notification_app import MacNotificationApp


class MacNotificationSender:
    """발송 직전 확정된 이름·아이콘을 반영하고 OS 요청 결과만 반환한다."""

    def __init__(
        self,
        app: MacNotificationApp,
        preferences: Callable[[], Preferences],
        authorize: Callable[[NotificationJob], bool],
    ) -> None:
        """같은 앱 어댑터와 읽기 전용 설정 클라이언트를 주입한다."""
        self._app = app
        self._preferences = preferences
        self._authorize = authorize

    def request(self, job: NotificationJob) -> DeliveryResult:
        """다른 발신 앱으로 우회하지 않고 기존 실행권 ID로 한 번만 요청한다."""
        try:
            return parse_delivery_result(self._app.deliver(job, self._preferences, self._authorize))
        except Exception:
            return "failed"
