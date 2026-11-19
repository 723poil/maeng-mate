"""알림 전용 번들에서 사용하는 UserNotifications API 계약이다."""

from collections.abc import Callable, Mapping

from Foundation import NSObject

UNNotificationDefaultActionIdentifier: str
UNAuthorizationOptionAlert: int
UNAuthorizationOptionSound: int
UNNotificationPresentationOptionBanner: int
UNNotificationPresentationOptionSound: int

class UNNotificationSound(NSObject):
    """기본 OS 알림 소리다."""
    @classmethod
    def defaultSound(cls) -> UNNotificationSound: ...

class UNMutableNotificationContent(NSObject):
    """배너에 표시할 제목·본문·소리다."""
    def setUserInfo_(self, value: Mapping[str, object]) -> None: ...
    def userInfo(self) -> Mapping[str, object]: ...
    def setTitle_(self, title: str) -> None: ...
    def setBody_(self, body: str) -> None: ...
    def setSound_(self, sound: UNNotificationSound) -> None: ...

class UNNotificationRequest(NSObject):
    """같은 작업 ID를 보존하는 즉시 알림 요청이다."""
    def content(self) -> UNMutableNotificationContent: ...
    @classmethod
    def requestWithIdentifier_content_trigger_(
        cls, identifier: str, content: UNMutableNotificationContent, trigger: None
    ) -> UNNotificationRequest: ...

class UNUserNotificationCenter(NSObject):
    """현재 번들의 OS 알림 요청 경계다."""
    @classmethod
    def currentNotificationCenter(cls) -> UNUserNotificationCenter: ...
    def getNotificationSettingsWithCompletionHandler_(
        self, callback: Callable[[UNNotificationSettings], None]
    ) -> None: ...
    def setDelegate_(self, delegate: NSObject) -> None: ...
    def requestAuthorizationWithOptions_completionHandler_(
        self, options: int, callback: Callable[[bool, object | None], None]
    ) -> None: ...
    def addNotificationRequest_withCompletionHandler_(
        self, request: UNNotificationRequest, callback: Callable[[object | None], None]
    ) -> None: ...

class UNNotificationSettings(NSObject):
    """현재 앱의 알림 허용 여부다."""
    def authorizationStatus(self) -> int: ...

class UNNotification(NSObject):
    """전달된 알림의 요청 내용을 읽는다."""
    def request(self) -> UNNotificationRequest: ...

class UNNotificationResponse(NSObject):
    """기본 클릭과 원본 알림을 구분한다."""
    def actionIdentifier(self) -> str: ...
    def notification(self) -> UNNotification: ...
