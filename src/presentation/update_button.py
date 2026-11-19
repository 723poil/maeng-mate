"""설정에서 현재 버전과 공개 업데이트를 확인한다."""

from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from importlib.metadata import version

from AppKit import NSAlert, NSAlertFirstButtonReturn, NSView
from Foundation import NSObject

from src.application.app_updates import AppUpdates
from src.presentation.native_views import button, label


class UpdateAction(NSObject):
    """네이티브 버튼 입력을 화면 객체에 전달한다."""

    owner: "UpdateButton"

    def checkUpdate_(self, sender: object) -> None:
        """사용자가 누른 확인 요청을 전달한다."""
        self.owner.check()


class UpdateButton:
    """배포 앱은 네이티브 설치를 열고 개발 실행본은 공개 버전을 안내한다."""

    def __init__(
        self,
        parent: NSView,
        actions: AppUpdates,
        pending: Callable[[], bool],
    ) -> None:
        """같은 버전 표시에 실행 환경이 지원하는 업데이트 동작을 연결한다."""
        self.current = version("maeng-mate")
        self.actions = actions
        self.target = UpdateAction.alloc().init()
        self.target.owner = self
        self.pending = pending
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="release-check")
        self.lookup: Future[str] | None = None
        label(parent, f"버전 {self.current}", 20, 35, 120, size=12, secondary=True)
        self.button = button(parent, "업데이트 확인…", 14, 7, 120, self.target, "checkUpdate:")

    def check(self) -> None:
        """사용자가 누른 경우에만 네트워크 조회를 시작한다."""
        if self.actions.install:
            try:
                self.actions.install(self.pending)
            except RuntimeError as error:
                self.show("업데이트를 확인하지 못했습니다.", str(error))
        elif self.lookup is None:
            self.button.setEnabled_(False)
            self.button.setTitle_("확인 중…")
            self.lookup = self.executor.submit(self.actions.latest)

    def tick(self) -> None:
        """공개 버전 조회 결과를 메인 스레드에서 받아 안내한다."""
        if self.lookup is None or not self.lookup.done():
            return
        lookup, self.lookup = self.lookup, None
        self.button.setEnabled_(True)
        self.button.setTitle_("업데이트 확인…")
        try:
            latest = lookup.result()
        except Exception:
            self.show(
                "업데이트를 확인하지 못했습니다.", "네트워크 연결을 확인한 뒤 다시 시도하세요."
            )
            return
        # 두 번호는 릴리스·빌드 경계에서 검증한 주·부·패치 버전이다.
        if tuple(map(int, latest.split("."))) <= tuple(map(int, self.current.split("."))):
            self.show(
                "설치할 새 버전이 없습니다.", f"설치 버전 {self.current} · 최신 공개 버전 {latest}"
            )
        else:
            self.show(
                f"새 버전 {latest}을 사용할 수 있습니다.",
                f"현재 버전은 {self.current}입니다. 릴리스에서 배포용 앱을 받을 수 있습니다.",
                download=True,
            )

    def show(self, title: str, message: str, *, download: bool = False) -> None:
        """확인 결과를 표시하고 사용자가 선택할 때만 릴리스 페이지를 연다."""
        alert = NSAlert.alloc().init()
        alert.setMessageText_(title)
        alert.setInformativeText_(message)
        if download:
            alert.addButtonWithTitle_("릴리스 열기")
            alert.addButtonWithTitle_("나중에")
        if alert.runModal() == NSAlertFirstButtonReturn and download:
            self.actions.open_release()

    def close(self) -> None:
        """창의 수명이 끝나면 네트워크 작업자를 정리한다."""
        self.executor.shutdown(wait=False, cancel_futures=True)
