"""화면 표시와 알림 구독을 독립적으로 표현하는 개인 설정이다."""

from collections.abc import Mapping
from dataclasses import dataclass, field

from src.domain.calendar import CalendarPreferences
from src.domain.notification_identity import NotificationIdentity
from src.domain.notification_timing import DEPARTURE_REMINDER_MINUTES
from src.domain.source_alerts import is_source_alert_kind

SECTIONS = ("work", "calendar")
LEGACY_SECTIONS = ("tasks", "qa", "meetings")
NOTIFICATION_TYPES = (
    "calendar",
    "task_assigned",
    "task_status",
    "task_due",
    "qa_assigned",
    "qa_status",
    "qa_due",
    "meeting",
    *(f"departure_{minutes}" for minutes in DEPARTURE_REMINDER_MINUTES),
    "sync_error",
)


@dataclass(frozen=True)
class SectionPreference:
    """숨김 상태와 무관하게 영역 순서와 표시 선택을 보존한다."""

    key: str
    visible: bool = True


@dataclass(frozen=True)
class Preferences:
    """현재 확정 버전과 화면·채널·알림 종류별 선택이다."""

    revision: int = 0
    sections: tuple[SectionPreference, ...] = tuple(
        SectionPreference(key, key != "calendar") for key in SECTIONS
    )
    notifications_enabled: bool = True
    channels: tuple[str, ...] = ("macos",)
    notification_types: tuple[str, ...] = NOTIFICATION_TYPES
    notification_identity: NotificationIdentity = NotificationIdentity()
    calendar: CalendarPreferences = CalendarPreferences()
    muted_source_notifications: Mapping[str, tuple[str, ...]] = field(default_factory=dict)

    def notification_enabled(self, kind: str, group: str | None) -> bool:
        """카테고리 선택이 없으면 이전 알림 설정을 이어받는다."""
        if group is not None and group in self.muted_source_notifications:
            return (
                kind in NOTIFICATION_TYPES or is_source_alert_kind(kind)
            ) and kind not in self.muted_source_notifications[group]
        return kind in self.notification_types or is_source_alert_kind(kind)

    def permits(self, channel: str, kinds: tuple[str, ...], group: str | None = None) -> bool:
        """화면 숨김과 별개로 전체·채널·종류가 모두 허용한 알림만 전달한다."""
        return (
            self.notifications_enabled
            and channel in self.channels
            and any(self.notification_enabled(kind, group) for kind in kinds)
        )
