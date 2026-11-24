"""알림 배너의 발신 앱 이름·아이콘을 편집하는 카드다."""

from dataclasses import replace

from AppKit import NSFont, NSImage, NSImageView, NSOpenPanel, NSTextField, NSView
from Foundation import NSData, NSNotification, NSObject

from src.application.notification_identity import validate_identity
from src.assets import APP_ICON
from src.domain.notification_identity import NotificationIdentity
from src.presentation.native_views import button, card, frame, label
from src.presentation.notification_identity_controller import NotificationIdentityController
from src.presentation.preferences_controller import PreferencesController
from src.presentation.settings_badge import SettingsBadge


class IdentityActions(NSObject):
    """편집과 명시적 적용·테스트 동작을 다른 알림 선택과 분리한다."""

    owner: "NotificationIdentityView"

    def controlTextDidChange_(self, notification: NSNotification) -> None:
        """이름 입력은 적용 전까지 화면 초안으로 유지한다."""
        self.owner.dirty = True
        self.owner.render()

    def choose_(self, sender: object) -> None:
        """네이티브 파일 선택에서 고른 이미지 한 개만 읽는다."""
        panel = NSOpenPanel.openPanel()
        panel.setCanChooseDirectories_(False)
        panel.setAllowsMultipleSelection_(False)
        panel.setAllowedFileTypes_(["png", "jpg", "jpeg", "icns"])
        panel.setPrompt_("아이콘 선택")
        if panel.runModal() == 1:
            url = panel.URL()
            if url is not None:
                self.owner.controller.choose_icon(url.path())
        self.owner.render()

    def reset_(self, sender: object) -> None:
        """이미지 선택과 이름을 기본값 초안으로 되돌린다."""
        self.owner.name.setStringValue_("Catch")
        self.owner._icon = None
        self.owner.dirty = True
        self.owner._preview()
        self.owner.render()

    def apply_(self, sender: object) -> None:
        """검증한 초안만 기존 설정 저장 흐름에 전달한다."""
        self.owner.apply()

    def test_(self, sender: object) -> None:
        """자동 테스트 발송 없이 사용자 버튼에서만 알림을 요청한다."""
        self.owner.controller.test()
        self.owner.render()


class NotificationIdentityView:
    """선택 이미지 미리보기와 짧은 폼을 같은 기준선에 배치한다."""

    def __init__(
        self,
        parent: NSView,
        top: float,
        preferences: PreferencesController,
        controller: NotificationIdentityController,
    ) -> None:
        """저장과 네이티브 반영 제어기를 별도로 주입한다."""
        self.preferences = preferences
        self.controller = controller
        self.dirty = False
        self._loaded: NotificationIdentity | None = None
        self._submitted: NotificationIdentity | None = None
        self._icon: bytes | None = None
        self._icon_generation = controller.icon_generation
        self._actions = IdentityActions.alloc().init()
        self._actions.owner = self
        card(parent, 16, top - 132, 552, 132)
        label(parent, "발신 앱", 28, top - 32, 160, size=13, bold=True)
        self._badge = SettingsBadge(parent, 456, top - 32, 100)
        self.preview = NSImageView.alloc().initWithFrame_(frame(28, top - 102, 54, 54))
        parent.addSubview_(self.preview)
        label(parent, "이름", 98, top - 63, 48, size=12, secondary=True)
        self.name = NSTextField.textFieldWithString_("")
        self.name.setFont_(NSFont.systemFontOfSize_(13))
        height = self.name.fittingSize()[1]
        self.name.setFrame_(frame(144, top - 68 + (28 - height) / 2, 300, height))
        self.name.setDelegate_(self._actions)
        parent.addSubview_(self.name)
        self._apply = button(parent, "적용", 456, top - 67, 100, self._actions, "apply:")
        self._choose = button(parent, "아이콘 선택", 98, top - 102, 112, self._actions, "choose:")
        self._reset = button(parent, "기본값", 218, top - 102, 80, self._actions, "reset:")
        self._test = button(parent, "알림 확인", 456, top - 102, 100, self._actions, "test:")
        self._preview()
        self.render()

    def _preview(self) -> None:
        """선택한 아이콘 데이터만 그리며 원본 파일을 다시 읽지 않는다."""
        image = (
            NSImage.alloc().initWithData_(NSData.dataWithBytes_length_(self._icon, len(self._icon)))
            if self._icon is not None
            else NSImage.alloc().initWithContentsOfFile_(str(APP_ICON))
        )
        self.preview.setImage_(image)

    def apply(self) -> None:
        """저장 성공 전에는 새 표시 정보를 발신 앱에 적용하지 않는다."""
        value = self.preferences.value
        if value is None or self.controller.busy:
            return
        try:
            identity = validate_identity(NotificationIdentity(self.name.stringValue(), self._icon))
        except ValueError as error:
            self.controller.error = str(error)
            self.render()
            return
        self.controller.retry()
        self._submitted = identity
        self.preferences.change(replace(value, notification_identity=identity))
        self.render()

    def render(self) -> None:
        """입력 중인 초안을 보존하고 저장·반영·권한 요청을 구분한다."""
        value = self.preferences.value
        confirmed = self.preferences.confirmed
        if (
            self._submitted is not None
            and confirmed is not None
            and confirmed.notification_identity == self._submitted
        ):
            # 저장 응답이 도착하기 전에 다시 편집한 이름·이미지는 새 초안으로 남긴다.
            self.dirty = (
                NotificationIdentity(self.name.stringValue().strip(), self._icon) != self._submitted
            )
            self._submitted = None
        if (
            value is not None
            and not self.dirty
            and self._submitted is None
            and self._loaded != value.notification_identity
        ):
            self._loaded = value.notification_identity
            self.name.setStringValue_(value.notification_identity.name)
            self._icon = value.notification_identity.icon_png
            self._preview()
        if self._icon_generation != self.controller.icon_generation:
            self._icon_generation = self.controller.icon_generation
            self._icon = self.controller.selected_icon
            self.dirty = True
            self._preview()
        enabled = value is not None and not self.preferences.loading and not self.controller.busy
        for control in (self.name, self._choose, self._reset):
            control.setEnabled_(enabled)
        self._apply.setEnabled_(enabled and (self.dirty or self.controller.error is not None))
        self._test.setEnabled_(
            enabled
            and not self.dirty
            and self._submitted is None
            and self.controller.applied is not None
            and self.controller.error is None
        )
        self._badge.update(
            "확인 필요"
            if self.controller.error
            else "처리 중"
            if self.controller.busy or self._submitted
            else "미적용"
            if self.dirty
            else "적용됨"
            if self.controller.applied
            else "준비 중",
            "orange"
            if self.controller.error
            else "blue"
            if self.controller.busy or self._submitted
            else "neutral"
            if self.dirty
            else "green",
        )
        self._badge.set_tooltip(self.controller.error or self.controller.message)
