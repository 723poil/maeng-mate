"""사용하는 PyObjC Foundation API의 최소 타입 계약이다."""

from typing import Self

class NSObject:
    """Cocoa 콜백 대상의 기반 객체다."""
    def performSelectorOnMainThread_withObject_waitUntilDone_(
        self, selector: str, value: object, wait: bool
    ) -> None: ...
    @classmethod
    def alloc(cls) -> Self:
        """객체 메모리를 할당한다."""
        ...
    def init(self) -> Self:
        """기본 객체를 초기화한다."""
        ...

class NSTimer(NSObject):
    """메인 run loop 타이머다."""
    @classmethod
    def scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
        cls, interval: float, target: NSObject, selector: str, info: object, repeats: bool
    ) -> NSTimer:
        """반복 콜백을 등록한다."""
        ...
    def invalidate(self) -> None:
        """타이머를 해제한다."""
        ...

NSRunLoopCommonModes: str

class NSRunLoop(NSObject):
    """입력 추적 중에도 전환 타이머를 실행하는 기본 루프다."""
    @classmethod
    def currentRunLoop(cls) -> NSRunLoop: ...
    def addTimer_forMode_(self, timer: NSTimer, mode: str) -> None: ...
    def runUntilDate_(self, date: NSDate) -> None: ...

class NSNotificationCenter(NSObject):
    """앱의 활성 상태 관찰을 등록·해제한다."""
    @classmethod
    def defaultCenter(cls) -> NSNotificationCenter: ...
    def addObserver_selector_name_object_(
        self, observer: NSObject, selector: str, name: str, sender: object
    ) -> None: ...
    def removeObserver_(self, observer: NSObject) -> None: ...

class NSIndexSet(NSObject):
    """사이드바 선택 행을 기본 컨트롤에 전달한다."""
    @classmethod
    def indexSetWithIndex_(cls, index: int) -> NSIndexSet: ...

class NSData(NSObject):
    """네이티브 보안 데이터의 버퍼 변환 계약이다."""
    def __bytes__(self) -> bytes: ...
    @classmethod
    def dataWithBytes_length_(cls, data: bytes, length: int) -> NSData: ...

class NSNotification(NSObject):
    """입력 변경을 보낸 네이티브 컨트롤을 구분한다."""
    def object(self) -> object: ...

class NSBundle(NSObject):
    """현재 실행 앱 번들의 식별자다."""
    @classmethod
    def mainBundle(cls) -> NSBundle: ...
    def bundleIdentifier(self) -> str | None: ...

class NSDate(NSObject):
    """런루프의 다음 처리 시각이다."""
    @classmethod
    def dateWithTimeIntervalSinceNow_(cls, seconds: float) -> NSDate: ...

class NSURL(NSObject):
    """검증한 링크 또는 파일 선택 결과의 주소다."""
    @classmethod
    def URLWithString_(cls, value: str) -> NSURL | None: ...
    def path(self) -> str: ...
