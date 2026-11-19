"""실행 경계에서 업무 코드에 주입하는 시스템 시계다."""

from datetime import UTC, datetime


def now() -> datetime:
    """현재 UTC 시점을 반환한다."""
    return datetime.now(UTC)
