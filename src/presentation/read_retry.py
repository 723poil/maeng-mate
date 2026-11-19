"""설정 화면에서 실패한 조회의 다음 시도를 예약한다."""

import time


class ReadRetry:
    """조회 실패가 이어지면 대기를 1초부터 최대 30초까지 늘린다."""

    def __init__(self) -> None:
        """처음에는 예약 없이 다음 실패부터 대기한다."""
        self._deadline: float | None = None
        self._delay = 1.0

    @property
    def waiting(self) -> bool:
        """재조회가 예약된 동안 저장 버튼을 잠글 수 있게 한다."""
        return self._deadline is not None

    @property
    def due(self) -> bool:
        """시스템 시각을 조정해도 대기 시간은 단조 시계로 판단한다."""
        return self._deadline is not None and time.monotonic() >= self._deadline

    def schedule(self) -> None:
        """이번 실패의 대기를 예약하고 다음 실패의 간격을 준비한다."""
        self._deadline = time.monotonic() + self._delay
        self._delay = min(30.0, self._delay * 2)

    def cancel(self) -> None:
        """예약을 해제하되 연속 실패의 대기 간격은 유지한다."""
        self._deadline = None

    def reset(self) -> None:
        """연속 실패가 끝나면 예약과 대기 간격을 초기화한다."""
        self._deadline = None
        self._delay = 1.0
