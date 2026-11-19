"""배포 앱에 동봉된 Sparkle로 서명된 업데이트를 확인하고 설치한다."""

from collections.abc import Callable
from pathlib import Path
from typing import Protocol, cast

from AppKit import NSAlert, NSApplication
from Foundation import NSBundle, NSObject


class StandardController(Protocol):
    """동봉 프레임워크에서 동적으로 불러오는 Sparkle 컨트롤러의 공개 계약이다."""

    def initWithUpdaterDelegate_userDriverDelegate_(
        self, delegate: NSObject, driver: None
    ) -> "StandardController":
        """메인 앱 번들의 수동 업데이트 컨트롤러를 초기화한다."""
        ...

    def checkForUpdates_(self, sender: object | None) -> None:
        """Sparkle 기본 창에서 조회·다운로드·검증·설치를 진행한다."""
        ...


def load_controller(framework: Path) -> StandardController:
    """배포 경로의 프레임워크만 열고 필요한 Objective-C 메서드를 확인한다."""
    bundle = NSBundle.bundleWithPath_(str(framework))
    if bundle is None or not bundle.load():
        raise RuntimeError("업데이트 도구를 열 수 없습니다. 앱을 다시 설치하세요.")
    controller = bundle.classNamed_("SPUStandardUpdaterController")
    if controller is None or not all(
        controller.instancesRespondToSelector_(selector)
        for selector in ("initWithUpdaterDelegate:userDriverDelegate:", "checkForUpdates:")
    ):
        raise RuntimeError("업데이트 도구를 초기화할 수 없습니다.")
    # PyObjC는 동적으로 메서드를 노출하므로 실제 Objective-C 선택자를 검사한 뒤 좁힌다.
    return cast(StandardController, controller.alloc())


class UpdateDelegate(NSObject):
    """설치 직전의 종료 요청에서 작성 중인 자료와 실행 중인 서버를 보호한다."""

    installing: bool
    pending: Callable[[], bool]
    stop_resident: Callable[[], None]

    def updater_willInstallUpdate_(self, updater: object, item: object) -> None:
        """조회·다운로드가 끝나 실제 설치가 결정된 시점만 표시한다."""
        self.installing = True

    def updater_didAbortWithError_(self, updater: object, error: object) -> None:
        """취소·실패 뒤 일반 메뉴바 종료가 서버를 중지하지 않게 한다."""
        self.installing = False

    def applicationShouldTerminate_(self, sender: NSApplication) -> int:
        """새 앱이 다시 시작할 수 있도록 저장과 서버 종료를 확인한다."""
        if not self.installing:
            return 1
        if self.pending():
            message = (
                "편집 중인 내용을 저장하거나 취소하고 진행 중인 작업을 마친 뒤 다시 시도하세요."
            )
        else:
            try:
                self.stop_resident()
            except (RuntimeError, ValueError, OSError):
                message = "상주 서버 종료를 확인하지 못했습니다. 잠시 뒤 다시 시도하세요."
            else:
                return 1
        alert = NSAlert.alloc().init()
        alert.setMessageText_("업데이트를 잠시 보류합니다.")
        alert.setInformativeText_(message)
        alert.runModal()
        return 0


class SparkleUpdates:
    """메뉴바가 실행되는 동안 약한 참조인 업데이트 델리게이트를 보관한다."""

    def __init__(self, framework: Path, stop_resident: Callable[[], None]) -> None:
        """설정에서 처음 확인할 때까지 프레임워크의 초기화를 미룬다."""
        self.framework = framework
        self.stop_resident = stop_resident
        self.controller: StandardController | None = None
        self.delegate: UpdateDelegate | None = None

    def check(self, pending: Callable[[], bool]) -> None:
        """컨트롤러를 재사용해 중복 설치를 피하고 사용자가 요청한 확인 창을 연다."""
        if self.controller is None:
            instance = load_controller(self.framework)
            self.delegate = UpdateDelegate.alloc().init()
            self.delegate.pending = pending
            self.delegate.installing = False
            self.delegate.stop_resident = self.stop_resident
            self.controller = instance.initWithUpdaterDelegate_userDriverDelegate_(
                self.delegate, None
            )
            NSApplication.sharedApplication().setDelegate_(self.delegate)
        self.controller.checkForUpdates_(None)
