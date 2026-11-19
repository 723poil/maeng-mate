"""바깥 장식 없이 유리 콘텐츠 하나만 표시하는 메뉴바 패널이다."""

from collections.abc import Callable

from AppKit import (
    NSApplication,
    NSApplicationDidResignActiveNotification,
    NSBackingStoreBuffered,
    NSButton,
    NSColor,
    NSEvent,
    NSEventMaskLeftMouseDown,
    NSEventMaskRightMouseDown,
    NSEventModifierFlagCommand,
    NSEventModifierFlagControl,
    NSEventModifierFlagOption,
    NSEventModifierFlagShift,
    NSFloatingWindowLevel,
    NSPanel,
    NSTextView,
    NSView,
    NSWindowAnimationBehaviorNone,
    NSWindowCollectionBehaviorFullScreenAuxiliary,
    NSWindowCollectionBehaviorMoveToActiveSpace,
    NSWindowStyleMaskBorderless,
    NSWindowStyleMaskClosable,
    NSWindowStyleMaskFullSizeContentView,
    NSWindowStyleMaskTitled,
    NSWindowTitleHidden,
)
from Foundation import NSNotificationCenter, NSObject
from objc import super

from src.presentation.native_views import frame


class KeyablePanel(NSPanel):
    """테두리 없는 창에서도 선택 입력과 Escape 키를 받는다."""

    def canBecomeKeyWindow(self) -> bool:
        """근무·주기 선택 컨트롤의 키보드 입력을 허용한다."""
        return True

    def canBecomeMainWindow(self) -> bool:
        """메뉴바 보조 화면을 일반 주 창으로 승격하지 않는다."""
        return False

    def performKeyEquivalent_(self, event: NSEvent) -> bool:
        """앱 메뉴가 없는 패널에서도 현재 입력의 macOS 기본 편집 명령을 실행한다."""
        modifiers = event.modifierFlags()
        # 메뉴가 없는 보조 창은 표준 닫기 동작을 직접 전달하고 열린 시트의 입력을 우선한다.
        if (
            self.styleMask() & NSWindowStyleMaskClosable
            and modifiers & NSEventModifierFlagCommand
            and not modifiers
            & (NSEventModifierFlagControl | NSEventModifierFlagOption | NSEventModifierFlagShift)
            and self.attachedSheet() is None
        ):
            key = (event.charactersIgnoringModifiers() or "").lower()
            if key == "w" or (not key.isascii() and event.keyCode() == 13):
                self.performClose_(None)
                return True
        editor = self.firstResponder()
        if (
            modifiers & NSEventModifierFlagCommand
            and not modifiers & (NSEventModifierFlagControl | NSEventModifierFlagOption)
            and isinstance(editor, NSTextView)
            and editor.isEditable()
        ):
            # 한글 입력기의 charactersIgnoringModifiers는 V 대신 ㅍ을 돌려줄 수 있다.
            # Command가 반영된 문자를 먼저 보고 비라틴 입력기는 실제 키 위치로 보완한다.
            key = (event.characters() or "").lower()
            if key not in {"a", "c", "v", "x"}:
                key = (event.charactersIgnoringModifiers() or "").lower()
            if key not in {"a", "c", "v", "x"}:
                key = {0: "a", 8: "c", 9: "v", 7: "x"}.get(event.keyCode(), "")
            action = (
                {
                    "a": "selectAll:",
                    "c": "copy:",
                    "v": "paste:",
                    "x": "cut:",
                }.get(key)
                if not modifiers & NSEventModifierFlagShift
                else None
            )
            # 보안 필드의 복사 제한·선택 교체·수정 알림은 Cocoa가 처리한다.
            if action and NSApplication.sharedApplication().sendAction_to_from_(
                action, editor, self
            ):
                return True
        return bool(super().performKeyEquivalent_(event))

    def cancelOperation_(self, sender: object) -> None:
        """Escape로 패널만 닫고 상주 앱은 유지한다."""
        if self.styleMask() & NSWindowStyleMaskClosable:
            if self.attachedSheet() is None:
                self.performClose_(sender)
        else:
            self.orderOut_(None)


def auxiliary_window(title: str, width: float, height: float) -> KeyablePanel:
    """독립 보조 창에는 AppKit이 그리는 닫기 버튼과 투명 제목 막대를 사용한다."""
    panel = KeyablePanel.alloc().initWithContentRect_styleMask_backing_defer_(
        frame(0, 0, width, height),
        NSWindowStyleMaskTitled | NSWindowStyleMaskClosable | NSWindowStyleMaskFullSizeContentView,
        NSBackingStoreBuffered,
        False,
    )
    panel.setTitle_(title)
    panel.setTitleVisibility_(NSWindowTitleHidden)
    panel.setTitlebarAppearsTransparent_(True)
    panel.setReleasedWhenClosed_(False)
    panel.setOpaque_(False)
    panel.setBackgroundColor_(NSColor.clearColor())
    panel.setBecomesKeyOnlyIfNeeded_(False)
    panel.setHidesOnDeactivate_(False)
    panel.setMovableByWindowBackground_(True)
    panel.center()
    return panel


class PanelEvents(NSObject):
    """외부 클릭으로 키 창을 잃으면 메뉴바 화면을 닫는다."""

    owner: "GlassPanel"

    def applicationDidResignActive_(self, notification: object) -> None:
        """앱이 비활성화되면 숨김 상태를 확정해 다음 클릭에서 다시 연다."""
        self.owner.hide()

    def windowDidResignKey_(self, notification: object) -> None:
        """다른 앱을 조작할 때 화면을 남기지 않는다."""
        self.owner.hide()


class GlassPanel:
    """한 번 정한 메뉴바 기준점을 유지하며 높이만 변경한다."""

    def __init__(
        self,
        content: NSView,
        width: float,
        height: float,
        *,
        on_hide: Callable[[], None] | None = None,
    ) -> None:
        """투명한 borderless 창 위에 네이티브 유리 뷰를 직접 연결한다."""
        self._width = width
        self._on_hide = on_hide
        self._hiding = False
        self._height = height
        self._outside_monitor: object | None = None
        self._left = 0.0
        self._top = height
        self._window = KeyablePanel.alloc().initWithContentRect_styleMask_backing_defer_(
            frame(0, 0, width, height), NSWindowStyleMaskBorderless, NSBackingStoreBuffered, False
        )
        self._window.setOpaque_(False)
        self._window.setBackgroundColor_(NSColor.clearColor())
        # 유리 소재와 별개인 창 그림자는 사각 창 경계를 따라 남으므로 겹치지 않는다.
        self._window.setHasShadow_(False)
        self._window.setLevel_(NSFloatingWindowLevel)
        self._window.setHidesOnDeactivate_(True)
        self._window.setBecomesKeyOnlyIfNeeded_(False)
        self._window.setCollectionBehavior_(
            NSWindowCollectionBehaviorMoveToActiveSpace
            | NSWindowCollectionBehaviorFullScreenAuxiliary
        )
        self._window.setAnimationBehavior_(NSWindowAnimationBehaviorNone)
        self._window.setReleasedWhenClosed_(False)
        self._window.setContentView_(content)
        self._events = PanelEvents.alloc().init()
        self._events.owner = self
        self._window.setDelegate_(self._events)
        NSNotificationCenter.defaultCenter().addObserver_selector_name_object_(
            self._events,
            "applicationDidResignActive:",
            NSApplicationDidResignActiveNotification,
            None,
        )

    def is_visible(self) -> bool:
        """현재 화면에 패널이 열려 있는지 확인한다."""
        return self._window.isVisible()

    def show(self, button: NSButton) -> None:
        """누른 메뉴바 버튼의 화면에서 기준점을 한 번만 정한다."""
        anchor_window = button.window()
        if anchor_window is None:
            return
        screen = anchor_window.screen()
        if screen is None:
            return
        anchor = anchor_window.convertRectToScreen_(
            button.convertRect_toView_(button.bounds(), None)
        )
        (screen_x, screen_y), (screen_width, screen_height) = screen.visibleFrame()
        (button_x, button_y), (button_width, _) = anchor
        self._left = max(
            screen_x + 8,
            min(
                button_x + button_width / 2 - self._width / 2,
                screen_x + screen_width - self._width - 8,
            ),
        )
        self._top = min(button_y - 6, screen_y + screen_height - 6)
        self.resize(self._width, self._height)
        self._window.makeKeyAndOrderFront_(None)
        if self._window.isVisible() and self._outside_monitor is None:
            self._outside_monitor = NSEvent.addGlobalMonitorForEventsMatchingMask_handler_(
                NSEventMaskLeftMouseDown | NSEventMaskRightMouseDown, self._clicked_outside
            )

    def resize(self, width: float, height: float) -> None:
        """크기 전환마다 팝오버의 위치·화살표 계산을 다시 실행하지 않는다."""
        self._width, self._height = width, height
        # 즉시 그리지 않고 같은 메인 루프에서 내부 배치까지 마친 뒤 함께 표시한다.
        self._window.setFrame_display_(frame(self._left, self._top - height, width, height), False)

    def _clicked_outside(self, event: NSEvent) -> None:
        """앱이 활성화되지 않았어도 다른 앱 클릭으로 닫고 이벤트는 읽거나 저장하지 않는다."""
        self.hide()

    def hide(self) -> None:
        """창과 유리 소재는 재사용하고 화면에서만 내린다."""
        if self._hiding:
            return
        self._hiding = True
        try:
            if self._on_hide is not None:
                self._on_hide()
            self._window.orderOut_(None)
            if self._outside_monitor is not None:
                NSEvent.removeMonitor_(self._outside_monitor)
                self._outside_monitor = None
        finally:
            self._hiding = False

    def close(self) -> None:
        """프로세스 종료 시 앱 활성 상태 관찰도 해제한다."""
        self.hide()
        NSNotificationCenter.defaultCenter().removeObserver_(self._events)
