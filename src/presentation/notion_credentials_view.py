"""Notion 인증의 보안 입력과 색상 상태 배지를 그린다."""

from AppKit import NSFont, NSSecureTextField, NSView
from Foundation import NSObject

from src.presentation.native_views import button, card, frame, label
from src.presentation.notion_credentials_controller import NotionCredentialsController
from src.presentation.settings_badge import SettingsBadge


class NotionCredentialsView:
    """서버의 토큰을 다시 채우지 않는 인증 전용 카드다."""

    def __init__(
        self, document: NSView, controller: NotionCredentialsController, actions: NSObject
    ) -> None:
        """사용자 ID 카드 아래에 같은 여백과 기본 높이로 배치한다."""
        self.controller = controller
        self._clear_generation = -1
        height = document.frame()[1][1]
        card(document, 16, height - 242, 552, 140)
        label(document, "인증 토큰", 28, height - 134, 200, size=13, bold=True)
        self._badge = SettingsBadge(document, 456, height - 134, 100)
        self.token = NSSecureTextField.textFieldWithString_("")
        self.token.setFont_(NSFont.systemFontOfSize_(13))
        field_height = min(28.0, self.token.fittingSize()[1])
        self.token.setFrame_(frame(28, height - 168 + (28 - field_height) / 2, 528, field_height))
        self.token.setPlaceholderString_("새 토큰")
        self.token.setDelegate_(actions)
        document.addSubview_(self.token)
        self.save = button(
            document, "확인 후 저장", 28, height - 199, 108, actions, "saveCredential:"
        )
        self.check = button(
            document, "연결 확인", 144, height - 199, 100, actions, "checkCredential:"
        )
        self.delete = button(document, "삭제", 486, height - 199, 70, actions, "deleteCredential:")
        self._detail = label(document, "", 28, height - 229, 528, size=12, secondary=True)
        self.render()

    def clear_confirmed_input(self) -> None:
        """창이 닫힌 동안에도 컨트롤러의 입력 폐기 요청을 반영한다."""
        if self._clear_generation != self.controller.clear_generation:
            self.token.setStringValue_("")
            self._clear_generation = self.controller.clear_generation

    def render(self) -> None:
        """확정 쓰기 뒤 입력을 비우고 진행·결과를 색상 배지로 표시한다."""
        controller = self.controller
        self.clear_confirmed_input()
        self.token.setEnabled_(controller.editable)
        self.save.setEnabled_(controller.editable and controller.input_dirty)
        configured = controller.state is not None and controller.state.configured
        self.check.setEnabled_(controller.editable and configured and not controller.input_dirty)
        self.delete.setEnabled_(controller.editable and configured and not controller.input_dirty)
        caption, tone = controller.badge
        self._badge.update(caption, tone)
        self._detail.setStringValue_(
            controller.error
            or (
                "저장 위치: Keychain"
                if controller.state is not None and controller.state.source == "keychain"
                else "환경변수 사용"
                if controller.state is not None and controller.state.source == "environment"
                else ""
            )
        )
