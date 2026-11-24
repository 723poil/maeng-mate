"""알림 카드 안에서 macOS 알림 권한과 발신 앱 준비를 확인하는 한 줄이다."""

from AppKit import NSView
from Foundation import NSObject

from src.presentation.native_views import (
    BUTTON_BEZEL_INSET,
    CARD_PADDING,
    CARD_WIDTH,
    FORM_CONTROL_X,
    FORM_LABEL_WIDTH,
    FORM_ROW,
    button,
    form_control_y,
    label,
    place_label,
)
from src.presentation.notification_identity_controller import NotificationIdentityController
from src.presentation.settings_badge import SettingsBadge

TEST_BUTTON_WIDTH = 88.0


class CheckActions(NSObject):
    """버튼 동작만 제어기에 전달한다."""

    owner: "NotificationCheckView"

    def test_(self, sender: object) -> None:
        """자동 발송 없이 사용자가 누를 때만 권한과 테스트 알림을 요청한다."""
        self.owner.controller.test()
        self.owner.render()


class NotificationCheckView:
    """발신 앱 이름·아이콘은 앱 기본값으로 고정하고 권한 확인만 남긴다."""

    def __init__(
        self, parent: NSView, y: float, controller: NotificationIdentityController
    ) -> None:
        """이름·상태 배지·확인 버튼을 알림 카드의 한 입력 줄에 둔다."""
        self.controller = controller
        self._actions = CheckActions.alloc().init()
        self._actions.owner = self
        caption = label(parent, "macOS 권한", 0, 0, FORM_LABEL_WIDTH - 8)
        place_label(caption, CARD_PADDING, y, FORM_LABEL_WIDTH - 8, FORM_ROW)
        self._badge = SettingsBadge(
            parent, FORM_CONTROL_X, form_control_y(y, 20), 120, align="left"
        )
        self._test = button(
            parent,
            "알림 확인",
            CARD_WIDTH - CARD_PADDING - TEST_BUTTON_WIDTH + BUTTON_BEZEL_INSET,
            form_control_y(y, 26),
            TEST_BUTTON_WIDTH,
            self._actions,
            "test:",
        )
        self.render()

    def render(self) -> None:
        """발신 앱 준비·권한 요청 결과를 배지로 표시한다."""
        controller = self.controller
        # 적용 실패 뒤에는 같은 버튼으로 다시 시도할 수 있게 열어 둔다.
        self._test.setEnabled_(
            not controller.busy and (controller.applied is not None or controller.error is not None)
        )
        self._badge.update(
            "확인 필요"
            if controller.error
            else "처리 중"
            if controller.busy
            else "준비됨"
            if controller.applied
            else "준비 중",
            "orange" if controller.error else "blue" if controller.busy else "green",
        )
        self._badge.set_tooltip(controller.error or controller.message)
