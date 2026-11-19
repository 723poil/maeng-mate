"""사용 가능한 전달 채널과 구현을 한 조립 지점에서 관리한다."""

from src.application.notifications import NotificationChannel, NotificationQueue, NotificationSender
from src.application.preferences import PreferencesClient
from src.infrastructure.macos_notification_app import MacNotificationApp
from src.infrastructure.macos_notifications import MacNotificationSender

CHANNELS = (NotificationChannel("macos", "macOS 알림"), NotificationChannel("slack", "Slack DM"))


def build_senders(
    app: MacNotificationApp,
    preferences: PreferencesClient,
    queue: NotificationQueue,
    slack: NotificationSender,
) -> dict[str, NotificationSender]:
    """구성 완료된 어댑터만 등록하며 외부 발송 채널을 자동 활성화하지 않는다."""
    return {"macos": MacNotificationSender(app, preferences.read, queue.authorize), "slack": slack}
