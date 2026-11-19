"""알림 발신 앱 반영과 이미지 선택이 UI를 막지 않게 관리한다."""

from concurrent.futures import Future, ThreadPoolExecutor

from src.application.notification_identity import NotificationBranding
from src.domain.notification_identity import NotificationIdentity


class NotificationIdentityController:
    """저장된 설정만 앱에 적용하고 실패한 적용을 자동 반복하지 않는다."""

    def __init__(self, branding: NotificationBranding) -> None:
        """네이티브 파일·등록 구현을 주입하고 OS 권한은 요청하지 않는다."""
        self._branding = branding
        self._executor = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="notification-identity"
        )
        self._applying: Future[None] | None = None
        self._reading: Future[bytes] | None = None
        self._testing: Future[str] | None = None
        self._attempted: NotificationIdentity | None = None
        self.applied: NotificationIdentity | None = None
        self.selected_icon: bytes | None = None
        self.icon_generation = 0
        self.error: str | None = None
        self.message = ""
        self._closed = False

    @property
    def busy(self) -> bool:
        """설정 반영·이미지 읽기·테스트의 동시 실행을 막는다."""
        return any(value is not None for value in (self._applying, self._reading, self._testing))

    def choose_icon(self, path: str) -> None:
        """선택한 이미지 한 개만 별도 작업자에서 읽는다."""
        if self._closed or self.busy:
            return
        self.error = None
        self.message = "이미지 읽는 중…"
        self._reading = self._executor.submit(self._branding.read_icon, path)

    def retry(self) -> None:
        """명시적 적용 버튼은 같은 저장값의 이전 실패도 다시 시도할 수 있다."""
        if not self.busy:
            self._attempted = None
            self.error = None

    def test(self) -> None:
        """저장된 표시 정보가 적용된 뒤에만 테스트를 허용한다."""
        if self._closed or self.busy or self.applied is None or self.error is not None:
            return
        self.message = "macOS에 테스트 알림 요청 중…"
        self._testing = self._executor.submit(self._branding.test)

    def tick(self, confirmed: NotificationIdentity | None, revision: int = 0) -> None:
        """작업 결과를 메인 스레드에 반영하고 새 확정값만 앱에 전달한다."""
        if self._closed:
            return
        if self._reading is not None and self._reading.done():
            future, self._reading = self._reading, None
            try:
                self.selected_icon = future.result()
                self.icon_generation += 1
                self.message = "선택한 아이콘을 쓰려면 ‘적용’을 누르세요."
            except Exception:
                self.error = "이미지를 열지 못했습니다. PNG, JPEG 또는 ICNS 파일을 선택하세요."
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
                self.applied = self._attempted
                self.message = "적용됨"
            except Exception:
                self.applied = None
                self.error = "알림 앱 설정을 적용하지 못했습니다. ‘적용’을 다시 누르세요."
        if confirmed is not None and not self.busy and confirmed != self._attempted:
            self._attempted = confirmed
            self.error = None
            self.message = "알림 앱에 적용 중…"
            self._applying = self._executor.submit(self._branding.apply, confirmed, revision)

    def close(self) -> None:
        """추가 작업을 중단하고 대기 중 작업을 취소한다."""
        self._closed = True
        self._executor.shutdown(wait=False, cancel_futures=True)
