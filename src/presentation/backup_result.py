"""메뉴바 재시작이 끝난 뒤 복원 결과를 독립 프로세스에서 표시한다."""

from AppKit import NSAlert, NSApplication, NSApplicationActivationPolicyAccessory


def show_restore_result(title: str, message: str) -> None:
    """복원을 요청한 사용자가 성공이나 복구 실패를 확인하게 한다."""
    app = NSApplication.sharedApplication()
    app.setActivationPolicy_(NSApplicationActivationPolicyAccessory)
    app.activateIgnoringOtherApps_(True)
    alert = NSAlert.alloc().init()
    alert.setMessageText_(title)
    alert.setInformativeText_(message)
    alert.addButtonWithTitle_("확인")
    alert.runModal()
