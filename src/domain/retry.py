"""수집 재시도 횟수와 대기 시간을 결정하는 순수 정책이다."""

from dataclasses import dataclass
from typing import Protocol


class RetryPolicy(Protocol):
    """일시 오류의 재시도 규칙을 저장 구현과 분리한다."""

    def delay(self, code: str, retries: int, retry_after: float | None) -> float | None:
        """다음 빠른 재시도가 없으면 None을 반환한다."""
        ...


@dataclass(frozen=True)
class TransientRetryPolicy:
    """네트워크·시간 초과·요청 제한만 세 번 빠르게 재시도한다."""

    delays: tuple[float, ...] = (15.0, 30.0, 60.0)

    def delay(self, code: str, retries: int, retry_after: float | None) -> float | None:
        """서버 대기 지시를 우선하되 비정상적으로 긴 값은 하루로 제한한다."""
        if code not in {"unavailable", "timeout", "rate_limited"} or not 0 <= retries < len(
            self.delays
        ):
            return None
        return max(self.delays[retries], min(retry_after or 0.0, 86400.0))
