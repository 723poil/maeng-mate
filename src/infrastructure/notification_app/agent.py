"""macOS 알림을 요청하고 사용자가 클릭한 Notion 원본만 연다."""

import json
import sys
import time
from collections.abc import Callable

from AppKit import NSApplication, NSApplicationActivationPolicyAccessory, NSWorkspace
from Foundation import NSURL, NSBundle, NSDate, NSObject, NSRunLoop, NSTimer
from UserNotifications import (
    UNAuthorizationOptionAlert,
    UNAuthorizationOptionSound,
    UNMutableNotificationContent,
    UNNotificationDefaultActionIdentifier,
    UNNotificationPresentationOptionBanner,
    UNNotificationPresentationOptionSound,
    UNNotificationRequest,
    UNNotificationResponse,
    UNNotificationSettings,
    UNNotificationSound,
    UNUserNotificationCenter,
)

from src.application.notion_links import notion_link


class NotificationDelegate(NSObject):
    """앱이 잠깐 활성화된 상태에서도 같은 배너·소리 정책을 사용한다."""

    receiving = False

    def userNotificationCenter_didReceiveNotificationResponse_withCompletionHandler_(
        self, center: object, response: UNNotificationResponse, handler: Callable[[], None]
    ) -> None:
        """기본 클릭에서만 원본을 열고 닫기·다른 응답은 무시한다."""
        target: str | None = None
        try:
            if response.actionIdentifier() == UNNotificationDefaultActionIdentifier:
                target = notion_link(
                    response.notification().request().content().userInfo().get("notionUrl")
                )
        finally:
            handler()
        # 알림 응답은 백그라운드에서 올 수 있으므로 앱 열기는 메인 스레드로 넘긴다.
        self.performSelectorOnMainThread_withObject_waitUntilDone_(
            "openNotification:", target, False
        )

    def openNotification_(self, value: object) -> None:
        """검증된 Notion HTTPS 주소를 기본 앱으로 열며 비밀값을 읽지 않는다."""
        target = notion_link(value)
        if target is not None:
            url = NSURL.URLWithString_(target)
            if url is not None:
                NSWorkspace.sharedWorkspace().openURL_(url)
        if self.receiving:
            NSApplication.sharedApplication().terminate_(None)

    def finish_(self, sender: object) -> None:
        """알림 응답이 없는 수동 실행도 제한 시간이 지나면 종료한다."""
        if self.receiving:
            NSApplication.sharedApplication().terminate_(None)

    def userNotificationCenter_willPresentNotification_withCompletionHandler_(
        self, center: object, notification: object, handler: Callable[[int], None]
    ) -> None:
        """알림이 전경 실행 때문에 조용히 생략되지 않게 한다."""
        handler(UNNotificationPresentationOptionBanner | UNNotificationPresentationOptionSound)


def wait_until(complete: Callable[[], bool], seconds: float) -> bool:
    """Objective-C 비동기 완료를 메인 런루프에서 제한 시간 동안 기다린다."""
    deadline = time.monotonic() + seconds
    while not complete() and time.monotonic() < deadline:
        NSRunLoop.currentRunLoop().runUntilDate_(NSDate.dateWithTimeIntervalSinceNow_(0.02))
    return complete()


def authorize(center: UNUserNotificationCenter) -> str:
    """앱 자신의 알림 권한만 요청하고 사용자 거부를 성공으로 바꾸지 않는다."""
    result: list[str] = []

    def completed(granted: bool, error: object | None) -> None:
        """OS 오류 원문은 stdout이나 로그에 포함하지 않는다."""
        result.append("failed" if error is not None else "requested" if granted else "denied")

    center.requestAuthorizationWithOptions_completionHandler_(
        UNAuthorizationOptionAlert | UNAuthorizationOptionSound, completed
    )
    if not wait_until(lambda: bool(result), 50):
        return "unknown"
    return result[0]


def authorization_status(center: UNUserNotificationCenter) -> str:
    """앱 자신의 OS 알림 권한만 읽고 권한 대화상자를 열지 않는다."""
    result: list[str] = []

    def completed(settings: UNNotificationSettings) -> None:
        """원문 없이 아직 미선택·거부·허용만 구분한다."""
        status = settings.authorizationStatus()
        result.append(
            "not_determined" if status == 0 else "denied" if status == 1 else "authorized"
        )

    center.getNotificationSettingsWithCompletionHandler_(completed)
    return result[0] if wait_until(lambda: bool(result), 5) else "unknown"


def deliver(center: UNUserNotificationCenter, value: dict[str, object]) -> str:
    """OS 큐에 요청된 사실만 반환하며 실제 배너 표시를 단정하지 않는다."""
    title, body, identifier = value.get("title"), value.get("body"), value.get("id")
    if not all(
        isinstance(item, str) and 0 < len(item) <= 1000 for item in (title, body, identifier)
    ):
        return "failed"
    if not isinstance(title, str) or not isinstance(body, str) or not isinstance(identifier, str):
        return "failed"
    content = UNMutableNotificationContent.alloc().init()
    content.setTitle_(title)
    content.setBody_(body)
    content.setSound_(UNNotificationSound.defaultSound())
    target = notion_link(value.get("url"))
    if target is not None:
        content.setUserInfo_({"notionUrl": target})
    request = UNNotificationRequest.requestWithIdentifier_content_trigger_(
        identifier, content, None
    )
    result: list[bool] = []

    def completed(error: object | None) -> None:
        """요청 완료와 알림 표시를 구분하고 OS 오류 객체를 보관하지 않는다."""
        result.append(error is None)

    center.addNotificationRequest_withCompletionHandler_(request, completed)
    if not wait_until(lambda: bool(result), 5):
        return "unknown"
    return "requested" if result[0] else "failed"


def main() -> None:
    """앱 번들에서만 작은 JSON 요청을 읽고 고정된 결과만 반환한다."""
    state = "failed"
    try:
        application = NSApplication.sharedApplication()
        application.setActivationPolicy_(NSApplicationActivationPolicyAccessory)
        if NSBundle.mainBundle().bundleIdentifier() not in {
            "local.maeng-mate.notifications",
            "local.maeng-mate.dev.notifications",
        }:
            raise ValueError
        delegate = NotificationDelegate.alloc().init()
        center = UNUserNotificationCenter.currentNotificationCenter()
        center.setDelegate_(delegate)
        application.setDelegate_(delegate)
        data = sys.stdin.buffer.read(16385) if sys.stdin is not None else b""
        if not data:
            # OS가 배너 클릭으로 재실행한 경우 stdin 요청 없이 응답 이벤트를 받는다.
            # delegate는 app.run이 앱 시작을 완료하기 전에 등록해야 한다.
            delegate.receiving = True
            NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
                10, delegate, "finish:", None, False
            )
            application.run()
            return
        raw: object = json.loads(data)
        if not isinstance(raw, dict) or len(data) > 16384:
            raise ValueError
        mode = raw.get("mode")
        if mode not in {"probe", "status", "authorize", "request"}:
            raise ValueError
        state = (
            authorize(center)
            if mode == "authorize"
            else authorization_status(center)
            if mode == "status"
            else "ready"
        )
        if mode == "request":
            # 권한 대화상자는 명시적 확인 버튼에서만 연다. 발송 승인 뒤 긴 대기를 넣지 않는다.
            state = deliver(center, raw)
    except Exception:
        state = "failed"
    if sys.stdout is not None:
        print(state, flush=True)


if __name__ == "__main__":
    main()
