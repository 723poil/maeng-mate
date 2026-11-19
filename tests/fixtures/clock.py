"""실제 대기 없이 예약 경계를 검증하는 시계다."""

from datetime import UTC, datetime


class Clock:
    """명시적으로 이동시키는 UTC 시계다."""

    def __init__(self, value: float = 1800000000.0) -> None:
        """테스트별 독립 시각을 보관한다."""
        self.value = value

    def __call__(self) -> float:
        """SQLite에 주입할 epoch 초를 반환한다."""
        return self.value

    def now(self) -> datetime:
        """업무 유스케이스가 사용할 UTC 시각을 반환한다."""
        return datetime.fromtimestamp(self.value, UTC)
