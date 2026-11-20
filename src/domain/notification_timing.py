"""일정 알림의 유효시간과 퇴근 전 알림 간격을 정의한다."""

from datetime import date, datetime, time

from src.domain.models import Period
from src.domain.work_dates import SEOUL


def departure_minutes(kind: str) -> int | None:
    """퇴근 당일에 지정할 수 있는 분 단위 구독 키를 읽는다."""
    prefix = "departure_"
    if not kind.startswith(prefix):
        return None
    digits = kind[len(prefix) :]
    if not (1 <= len(digits) <= 4 and digits.isascii() and digits.isdecimal()):
        return None
    minutes = int(digits)
    return minutes if minutes < 24 * 60 and digits == str(minutes) else None


def calendar_alert_expiry(period: Period | None, now: datetime) -> float | None:
    """새 일정 알림은 종료 시각과 수집 후 24시간 중 먼저 오는 시점에 만료한다."""
    latest = now.timestamp() + 86400
    if period is None:
        return latest
    final = period.end or period.start
    if period.kind == "date":
        # 종일 일정은 서울 기준 마지막 날이 끝날 때까지 유효하다.
        day = date.fromisoformat(final)
        if day < now.astimezone(SEOUL).date():
            return None
        end = datetime.combine(day, time.max, SEOUL)
    else:
        end = datetime.fromisoformat(final)
        if end < now:
            return None
    return min(latest, end.timestamp())
