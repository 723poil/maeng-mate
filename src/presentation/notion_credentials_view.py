"""Notion 인증의 보안 입력과 색상 상태 배지를 그린다."""

from AppKit import (
    NSComboButton,
    NSComboButtonStyleSplit,
    NSFont,
    NSMenu,
    NSMenuItem,
    NSSecureTextField,
    NSView,
)
from Foundation import NSObject

from src.presentation.native_views import (
    BUTTON_BEZEL_INSET,
    CARD_GAP,
    CARD_PADDING,
    CARD_WIDTH,
    FORM_CONTROL_X,
    button,
    card,
    card_height,
    form_control_y,
    form_dividers,
    form_row_y,
    frame,
)
from src.presentation.notion_credentials_controller import NotionCredentialsController
from src.presentation.runtime_settings_form import (
    ACCOUNT_HEIGHT,
    FIELD_HEIGHT,
    PAGE_TOP,
    card_title,
    row_label,
)
from src.presentation.settings_badge import SettingsBadge

# 토큰 카드는 입력·상태 두 줄이며 계정 카드 바로 아래에 온다.
TOKEN_HEIGHT = card_height(2)
TOKEN_TOP = PAGE_TOP + ACCOUNT_HEIGHT + CARD_GAP
# 버튼 폭은 글자가 잘리지 않는 최소 폭이다.
SAVE_WIDTH = 108.0
CHECK_WIDTH = 96.0
DELETE_WIDTH = 64.0
CREATE_WIDTH = 168.0


class NotionCredentialsView:
    """서버의 토큰을 다시 채우지 않는 인증 전용 카드다."""

    def __init__(
        self, document: NSView, controller: NotionCredentialsController, actions: NSObject
    ) -> None:
        """계정 카드 아래에 토큰 입력·상태 줄을 놓는다."""
        self.controller = controller
        self._clear_generation = -1
        height = TOKEN_HEIGHT
        section = NSView.alloc().initWithFrame_(
            frame(CARD_PADDING, document.frame()[1][1] - TOKEN_TOP - height, CARD_WIDTH, height)
        )
        document.addSubview_(section)
        card(section, 0, 0, CARD_WIDTH, height)
        form_dividers(section, height, 2)
        card_title(section, "연결 토큰", height)

        # 첫 줄: 새 토큰 입력과 바로 옆 저장. 입력칸은 버튼 테두리 앞 8pt에서 끝난다.
        row = form_row_y(height, 0)
        row_label(section, "토큰", row)
        save_x = CARD_WIDTH - CARD_PADDING - SAVE_WIDTH + BUTTON_BEZEL_INSET
        self.token = NSSecureTextField.textFieldWithString_("")
        self.token.setFrame_(
            frame(
                FORM_CONTROL_X,
                form_control_y(row, FIELD_HEIGHT),
                save_x + BUTTON_BEZEL_INSET - 8 - FORM_CONTROL_X,
                FIELD_HEIGHT,
            )
        )
        self.token.setPlaceholderString_("새 토큰 붙여넣기")
        self.token.setDelegate_(actions)
        section.addSubview_(self.token)
        self.save = button(
            section,
            "확인 후 저장",
            save_x,
            form_control_y(row, 26),
            SAVE_WIDTH,
            actions,
            "saveCredential:",
        )

        # 둘째 줄: 상태 배지는 입력 시작선에, 저장된 토큰용 버튼은 오른쪽 끝에 둔다.
        row = form_row_y(height, 1)
        row_label(section, "상태", row)
        self._badge = SettingsBadge(
            section, FORM_CONTROL_X, form_control_y(row, 20), 120, align="left"
        )
        delete_x = CARD_WIDTH - CARD_PADDING - DELETE_WIDTH + BUTTON_BEZEL_INSET
        self.delete = button(
            section,
            "삭제",
            delete_x,
            form_control_y(row, 26),
            DELETE_WIDTH,
            actions,
            "deleteCredential:",
        )
        self.check = button(
            section,
            "연결 확인",
            delete_x - CHECK_WIDTH,
            form_control_y(row, 26),
            CHECK_WIDTH,
            actions,
            "checkCredential:",
        )
        # 토큰이 없거나 만료되면 연결 확인 대신 만들기 버튼을 둔다. 메뉴의 통합 연결은
        # 개인 토큰 생성이 막힌 회사 워크스페이스용 대안이다.
        alternative = NSMenu.alloc().initWithTitle_("토큰 만들기")
        connection = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
            "통합 연결로 만들기…", "createConnectionToken:", ""
        )
        connection.setTarget_(actions)
        alternative.addItem_(connection)
        self.create = NSComboButton.comboButtonWithTitle_menu_target_action_(
            "Notion에서 만들기…", alternative, actions, "createPersonalToken:"
        )
        self.create.setStyle_(NSComboButtonStyleSplit)
        self.create.setFont_(NSFont.systemFontOfSize_(12))
        self.create.setToolTip_("토큰을 만들고 Copy를 누르면 자동으로 연결합니다.")
        self._row_y = row
        section.addSubview_(self.create)
        self.cancel = button(
            section,
            "취소",
            delete_x,
            form_control_y(row, 26),
            DELETE_WIDTH,
            actions,
            "cancelTokenCapture:",
        )
        self.capturing = False
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
        # 감지 중에는 취소만, 토큰이 없거나 만료되면 만들기, 정상 토큰은 확인·삭제를 둔다.
        expired = configured and (
            controller.error_code == "unauthorized"
            or (controller.state is not None and controller.state.verification == "failed")
        )
        needs_token = not self.capturing and (not configured or expired)
        self.cancel.setHidden_(not self.capturing)
        self.create.setHidden_(not needs_token)
        self.create.setEnabled_(controller.editable)
        self.check.setHidden_(self.capturing or needs_token)
        self.delete.setHidden_(self.capturing or not configured)
        # 콤보 버튼은 테두리 안쪽 여백이 일반 버튼보다 1.5pt 좁아 오른쪽 끝을 그만큼 늘린다.
        right = CARD_WIDTH - CARD_PADDING - (DELETE_WIDTH if configured else 0) + 1.5
        self.create.setFrame_(
            frame(right - CREATE_WIDTH, form_control_y(self._row_y, 24), CREATE_WIDTH, 24)
        )
        caption, tone = controller.badge
        if self.capturing and not controller.busy:
            caption, tone = "복사 기다리는 중", "blue"
        self._badge.update(caption, tone)
        # 오류와 토큰 위치는 화면 줄 대신 배지 도움말로 알려 준다.
        source = controller.state.source if controller.state is not None else None
        self._badge.set_tooltip(
            controller.error
            or {
                "keychain": "Keychain에 저장되어 있습니다.",
                "environment": "환경변수의 토큰을 쓰고 있습니다.",
                "none": "Notion 통합에서 발급한 토큰을 붙여 넣으세요.",
            }.get(source or "", "")
        )
