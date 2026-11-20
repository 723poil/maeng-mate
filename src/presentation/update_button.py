"""설정에서 현재 버전과 공개 업데이트를 확인한다."""

import logging
import time
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from importlib.metadata import version

from AppKit import NSAlert, NSApplication, NSButton, NSView
from Foundation import NSObject

from src.application.app_updates import AppUpdates
from src.presentation.native_views import button, label


class UpdateAction(NSObject):
    """네이티브 버튼 입력을 화면 객체에 전달한다."""

    owner: "UpdateChecks"

    def checkUpdate_(self, sender: object) -> None:
        """사용자가 누른 확인 요청을 전달한다."""
        self.owner.check()

    def openRelease_(self, sender: object) -> None:
        """사용자가 선택한 경우에만 릴리스 페이지를 연다."""
        self.owner.dismiss()
        self.owner.actions.open_release()

    def dismissUpdate_(self, sender: object) -> None:
        """이번 안내를 닫고 다음 자동 조회를 기다린다."""
        self.owner.dismiss()


class UpdateChecks:
    """배포 앱은 네이티브 설치를 열고 개발 실행본은 공개 버전을 안내한다."""

    def __init__(
        self,
        actions: AppUpdates,
        pending: Callable[[], bool],
    ) -> None:
        """설정 창의 표시 여부와 무관하게 메뉴바에서 조회 상태를 보관한다."""
        self.current = version("maeng-mate")
        self.actions = actions
        self.target = UpdateAction.alloc().init()
        self.target.owner = self
        self.pending = pending
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="release-check")
        self.lookup: Future[str] | None = None
        self.alert: NSAlert | None = None
        self.button: NSButton | None = None
        self.manual = False
        self.next_check = 0.0

    def attach(self, parent: NSView) -> None:
        """설정을 처음 열 때 기존 조회에 수동 확인 버튼을 연결한다."""
        version_text = f"버전 {self.current}"
        if self.actions.is_development:
            version_text += " · 개발용"
        label(
            parent,
            version_text,
            20,
            35,
            155,
            size=12,
            secondary=True,
        )
        self.button = button(parent, "업데이트 확인…", 14, 7, 120, self.target, "checkUpdate:")

    def start(self) -> None:
        """서명된 배포 앱은 Sparkle에 조회 일정을 맡긴다."""
        if self.actions.native:
            try:
                self.actions.native.start(self.pending)
            except RuntimeError:
                # 자동 확인 초기화 실패로 메뉴바를 종료하지 않고 수동 재시도를 허용한다.
                logging.getLogger(__name__).warning("자동 업데이트 확인을 시작하지 못했습니다.")

    def check(self) -> None:
        """수동 요청은 진행 중인 자동 조회와 결과를 공유한다."""
        if self.actions.native:
            try:
                self.actions.native.check(self.pending)
            except RuntimeError as error:
                self.show("업데이트를 확인하지 못했습니다.", str(error))
        else:
            self.manual = True
            if self.button:
                self.button.setEnabled_(False)
                self.button.setTitle_("확인 중…")
            self._lookup_release()

    def _lookup_release(self) -> None:
        """수동 확인 직후 자동 조회가 겹치지 않도록 다음 조회 시각을 함께 갱신한다."""
        if self.lookup is None:
            self.next_check = time.monotonic() + 24 * 60 * 60
            self.lookup = self.executor.submit(self.actions.latest)

    def tick(self) -> None:
        """개발 실행본은 첫 틱과 이후 하루 간격으로 최신 정식 릴리스를 확인한다."""
        if self.actions.native:
            return
        if self.lookup is None and time.monotonic() >= self.next_check:
            self._lookup_release()
        if self.lookup is None or not self.lookup.done():
            return
        lookup, self.lookup = self.lookup, None
        manual, self.manual = self.manual, False
        if self.button and manual:
            self.button.setEnabled_(True)
            self.button.setTitle_("업데이트 확인…")
        try:
            latest = lookup.result()
        except Exception:
            if manual:
                self.show(
                    "업데이트를 확인하지 못했습니다.", "네트워크 연결을 확인한 뒤 다시 시도하세요."
                )
            else:
                logging.getLogger(__name__).warning("자동 업데이트 조회에 실패했습니다.")
            return
        # 두 번호는 릴리스·빌드 경계에서 검증한 주·부·패치 버전이다.
        if tuple(map(int, latest.split("."))) <= tuple(map(int, self.current.split("."))):
            if manual:
                self.show(
                    "설치할 새 버전이 없습니다.",
                    f"현재 실행 중인 개발용 버전 {self.current} · 최신 버전 {latest}",
                )
        else:
            self.show(
                f"{latest} 버전이 나왔습니다.",
                f"현재 개발용 {self.current} 버전을 사용 중입니다. ‘릴리스 열기’를 눌러 새 버전을 다운로드하세요.",
                download=True,
            )

    def show(self, title: str, message: str, *, download: bool = False) -> None:
        """확인 결과를 표시하고 사용자가 선택할 때만 릴리스 페이지를 연다."""
        alert = NSAlert.alloc().init()
        alert.setMessageText_(title)
        alert.setInformativeText_(message)
        NSApplication.sharedApplication().activateIgnoringOtherApps_(True)
        if not download:
            alert.runModal()
            return
        # 선택을 기다리는 동안에도 메뉴바의 자동 저장과 종료 요청을 계속 처리한다.
        self.dismiss()
        self.alert = alert
        for title, action in (("릴리스 열기", "openRelease:"), ("나중에", "dismissUpdate:")):
            choice = alert.addButtonWithTitle_(title)
            choice.setTarget_(self.target)
            choice.setAction_(action)
        # 비모달 창은 직접 배치해야 본문과 실제 버튼에 맞게 기본 뷰가 정리된다.
        alert.layout()
        alert.window().center()
        alert.window().makeKeyAndOrderFront_(None)

    def dismiss(self) -> None:
        """열려 있는 릴리스 안내 창을 정리한다."""
        if self.alert is not None:
            self.alert.window().orderOut_(None)
            self.alert = None

    def close(self) -> None:
        """메뉴바의 수명이 끝나면 안내 창과 네트워크 작업자를 정리한다."""
        self.dismiss()
        self.executor.shutdown(wait=False, cancel_futures=True)
