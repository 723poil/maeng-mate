"""알림 전달이 UI와 출근 자동 저장을 막지 않게 실행한다."""

import threading

from src.application.notifications import NotificationRunner


class NotificationWorker:
    """작업 실패는 짧은 상태로 노출하고 원문을 로그에 남기지 않는다."""

    def __init__(self, runner: NotificationRunner) -> None:
        """OS와 큐 구현은 조립부에서 주입한다."""
        self._runner = runner
        self._stop = threading.Event()
        self._error: str | None = None
        self._thread = threading.Thread(
            target=self._run, name="notification-deliveries", daemon=True
        )

    def start(self) -> None:
        """별도 작업자 하나만 시작한다."""
        self._thread.start()

    def _run(self) -> None:
        """한 번 요청한 작업을 재전송하지 않고 다음 due 작업을 확인한다."""
        while not self._stop.is_set():
            try:
                failed = self._runner.run_once()
                self._error = "알림 요청 확인 필요" if failed else None
            except Exception:
                self._error = "알림 연결 확인 필요"
            self._stop.wait(5)

    def status(self) -> str | None:
        """GUI가 메인 스레드에서 읽을 안전한 상태만 반환한다."""
        return self._error

    def close(self) -> None:
        """새 요청을 중지하고 짧게 정리 시간을 준다."""
        self._stop.set()
        self._thread.join(timeout=1)
