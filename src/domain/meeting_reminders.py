"""외부 저장소와 무관한 회의 시작 전 알림 정책이다."""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol

from src.domain.models import CalendarEvent
from src.domain.work_dates import canonical_identity


@dataclass(frozen=True)
class MeetingTiming:
    """알림 예정 시각과 더 이상 보내면 안 되는 회의 시작 시각이다."""

    due: datetime
    start: datetime


class MeetingReminderPolicy(Protocol):
    """회사별 참석 대상과 사전 알림 시점을 교체한다."""

    def schedule(self, event: CalendarEvent, user_id: str, now: datetime) -> MeetingTiming | None:
        """참석할 미래 회의일 때만 알림 시각을 반환한다."""
        ...


class TenMinuteMeetingReminderPolicy:
    """본인이 참석하는 시각 지정 회의를 시작 10분 전에 알린다."""

    def schedule(self, event: CalendarEvent, user_id: str, now: datetime) -> MeetingTiming | None:
        """종일·취소·본인 제외·시작한 일정은 임의 시각을 만들지 않는다."""
        if (
            event.event_type != "meeting"
            or event.cancelled
            or event.period is None
            or event.period.kind != "instant"
            or canonical_identity(user_id)
            not in {canonical_identity(person) for person in event.participant_ids}
        ):
            return None
        start = datetime.fromisoformat(event.period.start)
        if start.tzinfo is None or now.tzinfo is None:
            raise ValueError("회의 시작과 현재 시각에는 시간대가 필요합니다.")
        start = start.astimezone(UTC)
        if start <= now:
            return None
        # 10분 안에 처음 확인한 회의는 시작 전까지 즉시 전달할 수 있다.
        return MeetingTiming(start - timedelta(minutes=10), start)
