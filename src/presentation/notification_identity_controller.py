"""알림 발신 앱 준비와 권한 확인이 UI를 막지 않게 관리한다."""

from concurrent.futures import Future, ThreadPoolExecutor

from src.application.notification_identity import NotificationBranding
from src.domain.notification_identity import NotificationIdentity


class NotificationIdentityController:
    """발신 앱을 앱 기본 이름·아이콘으로 한 번 준비하고 실패를 자동 반복하지 않는다."""

    def __init__(self, branding: NotificationBranding) -> None:
        """네이티브 파일·등록 구현을 주입하고 OS 권한은 요청하지 않는다."""
        self._branding = branding
        self._executor = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="notification-identity"
        )
        self._applying: Future[None] | None = None
        self._testing: Future[str] | None = None
        self._attempted = False
        self.applied: NotificationIdentity | None = None
        self.error: str | None = None
        self.message = ""
        self._closed = False

    @property
    def busy(self) -> bool:
        """발신 앱 준비와 테스트의 동시 실행을 막는다."""
        return self._applying is not None or self._testing is not None

    def test(self) -> None:
        """준비에 실패했으면 다시 준비하고, 준비됐으면 테스트 알림을 요청한다."""
        if self._closed or self.busy:
            return
        if self.error is not None:
            # 다음 tick에서 기본값 적용을 다시 시도한다.
            self._attempted = False
            self.error = None
            return
        if self.applied is None:
            return
        self.message = "macOS에 테스트 알림 요청 중…"
        self._testing = self._executor.submit(self._branding.test)

    def tick(self) -> None:
        """작업 결과를 메인 스레드에 반영하고 처음 한 번만 기본 발신 앱을 준비한다."""
        if self._closed:
            return
        if self._testing is not None and self._testing.done():
            testing, self._testing = self._testing, None
            try:
                state = testing.result()
                self.message = (
                    "macOS에 테스트 알림을 요청했습니다."
                    if state == "requested"
                    else "macOS 설정에서 이 앱의 알림을 허용하세요."
                    if state == "denied"
                    else "테스트 알림이 도착했는지 확인하세요."
                    if state == "unknown"
                    else "macOS에서 이 앱의 알림 설정을 확인하세요."
                )
            except Exception:
                self.error = "알림 요청을 확인하지 못했습니다."
        if self._applying is not None and self._applying.done():
            applying, self._applying = self._applying, None
            try:
                applying.result()
                self.applied = NotificationIdentity()
                self.message = "준비됨"
            except Exception:
                self.applied = None
                self.error = "알림 앱을 준비하지 못했습니다. ‘알림 확인’을 눌러 다시 시도하세요."
        # 예전에 바꾼 이름·아이콘이 저장돼 있어도 앱 기본값으로 되돌린다.
        if not self._attempted and not self.busy:
            self._attempted = True
            self.error = None
            self.message = "알림 앱 준비 중…"
            self._applying = self._executor.submit(self._branding.apply, NotificationIdentity(), 0)

    def close(self) -> None:
        """추가 작업을 중단하고 대기 중 작업을 취소한다."""
        self._closed = True
        self._executor.shutdown(wait=False, cancel_futures=True)
