"""Slack DM 받을 계정·봇 토큰·앱 토큰을 설정 간격 체계의 카드로 나눈 화면이다."""

from AppKit import (
    NSButton,
    NSFont,
    NSLineBreakByTruncatingTail,
    NSScrollView,
    NSSecureTextField,
    NSTextField,
    NSView,
)
from Foundation import NSNotification, NSObject

from src.application.credentials import CredentialError, validated_secret
from src.application.slack_settings import validated_app_token
from src.presentation.native_views import (
    BUTTON_BEZEL_INSET,
    CARD_GAP,
    CARD_PADDING,
    CARD_TITLE,
    CARD_WIDTH,
    FORM_CONTROL_X,
    FORM_LABEL_WIDTH,
    FORM_ROW,
    button,
    card,
    card_height,
    form_control_y,
    form_dividers,
    form_row_y,
    frame,
    label,
    place_label,
)
from src.presentation.registration_status import registration_status
from src.presentation.settings_badge import SettingsBadge
from src.presentation.slack_settings_controller import SlackSettingsController

# 페이지 맨 위 카드와 스크롤 영역 위쪽 사이 여백이다.
PAGE_TOP = 8.0
# 시스템 입력칸과 테두리 버튼의 기본 높이다.
FIELD_HEIGHT = 22.0
BUTTON_HEIGHT = 26.0
# 카드 제목 줄 오른쪽 상태 배지가 차지할 수 있는 최대 폭이다.
BADGE_SLOT = 100.0
# 저장·삭제 버튼 폭이다. 세 카드의 저장 버튼이 같은 세로선에 오도록 한 값으로 맞춘다.
ACTION_WIDTH = 64.0
# 삭제는 카드 오른쪽 끝에 붙이고 저장은 그 바로 왼쪽 칸에 둔다(테두리가 프레임 안쪽에 그려진다).
DELETE_X = CARD_WIDTH - CARD_PADDING - ACTION_WIDTH + BUTTON_BEZEL_INSET
SAVE_X = DELETE_X - ACTION_WIDTH
# 입력칸은 저장 버튼 테두리의 8pt 앞에서 끝난다.
FIELD_WIDTH = SAVE_X + BUTTON_BEZEL_INSET - 8 - FORM_CONTROL_X
# 연결 확인·테스트 알림은 저장·삭제 두 칸을 합친 자리를 쓴다.
WIDE_WIDTH = 2 * ACTION_WIDTH
# 각 카드의 줄 수다. 문서 높이를 한 번에 계산하려고 상수로 둔다.
MEMBER_ROWS, BOT_ROWS, APP_ROWS = 1, 3, 1
# 저장된 토큰은 되채우지 않으므로 빈 입력칸에 교체 방법만 보인다.
REPLACE_HINT = "바꾸려면 새 토큰을 입력하세요"


class SlackActions(NSObject):
    """Slack 입력이 다른 프로바이더 초안으로 처리되지 않게 한다."""

    owner: "SlackSettingsView"

    def controlTextDidChange_(self, notification: NSNotification) -> None:
        """멤버 ID와 보안 입력을 각각 수정 상태로 남긴다."""
        controller = self.owner.controller
        if notification.object() == self.owner.app_token:
            controller.app_dirty = bool(self.owner.app_token.stringValue())
            controller.error = None
            controller.error_code = None
        else:
            controller.edited(token=notification.object() == self.owner.token)
            controller.token_dirty = bool(self.owner.token.stringValue())
        self.owner.render()

    def saveMember_(self, sender: object) -> None:
        """개인 DM 수신자만 명시적으로 저장한다."""
        self.owner.controller.save_member(self.owner.member.stringValue())
        self.owner.render()

    def saveToken_(self, sender: object) -> None:
        """보안 입력은 사용자 저장 요청에서만 읽는다."""
        try:
            self.owner.controller.credential(
                "token", validated_secret(self.owner.token.stringValue())
            )
        except CredentialError:
            self.owner.controller.invalid_token()
        self.owner.render()

    def check_(self, sender: object) -> None:
        """빈 입력 칸이 아니라 저장된 토큰을 확인한다."""
        self.owner.controller.credential("check")
        self.owner.render()

    def test_(self, sender: object) -> None:
        """저장된 본인에게 테스트 알림 한 건을 요청한다."""
        self.owner.controller.test_notification()
        self.owner.render()

    def delete_(self, sender: object) -> None:
        """일반 설정은 유지하고 토큰만 삭제한다."""
        self.owner.controller.credential("delete")
        self.owner.render()

    def saveAppToken_(self, sender: object) -> None:
        """앱 토큰은 형식 확인 후 저장하며 연결을 시작하지 않는다."""
        try:
            self.owner.controller.app_credential(
                validated_app_token(self.owner.app_token.stringValue())
            )
        except CredentialError:
            self.owner.controller.invalid_token(app=True)
        self.owner.render()

    def deleteAppToken_(self, sender: object) -> None:
        """앱 토큰만 삭제하고 봇 토큰은 유지한다."""
        self.owner.controller.app_credential()
        self.owner.render()


class SlackSettingsView:
    """DM 받을 계정·봇 토큰·앱 토큰을 각각 독립 저장하는 세 카드다."""

    def __init__(
        self, page: NSScrollView, viewport: float, controller: SlackSettingsController
    ) -> None:
        """독립 설정과 사용자가 누른 테스트 발송을 제공한다."""
        self.controller = controller
        self._member_generation = -1
        self._clear_generation = -1
        self._app_clear_generation = -1
        self._actions = SlackActions.alloc().init()
        self._actions.owner = self
        content = (
            PAGE_TOP
            + card_height(MEMBER_ROWS)
            + CARD_GAP
            + card_height(BOT_ROWS)
            + CARD_GAP
            + card_height(APP_ROWS)
            + CARD_PADDING
        )
        height = max(content, viewport)
        document = NSView.alloc().initWithFrame_(frame(0, 0, 584, height))
        page.setDocumentView_(document)
        top = height - PAGE_TOP

        # DM 받을 계정: 멤버 ID 입력과 찾는 위치 안내.
        member, member_height, self._member_badge = self._card(
            document, top, "DM 받을 계정", MEMBER_ROWS
        )
        row = form_row_y(member_height, 0)
        self._row_label(member, "멤버 ID", row)
        self.member = self._field(member, row, secure=False)
        self.member.setPlaceholderString_("U로 시작하는 ID")
        # 찾는 곳은 화면 줄 대신 입력칸 도움말로만 알려 준다.
        self.member.setToolTip_("Slack 내 프로필 → 더보기(⋮) → 멤버 ID 복사")
        self._member_save = self._button(member, "저장", SAVE_X, row, ACTION_WIDTH, "saveMember:")
        top -= member_height + CARD_GAP

        # 봇 토큰: 저장·삭제, 연결 확인, 본인 테스트 DM.
        bot, bot_height, self._token_badge = self._card(document, top, "봇 토큰", BOT_ROWS)
        row = form_row_y(bot_height, 0)
        self._row_label(bot, "토큰", row)
        self.token = self._field(bot, row, secure=True)
        self._token_save = self._button(bot, "저장", SAVE_X, row, ACTION_WIDTH, "saveToken:")
        self._token_save.setToolTip_("Slack에 토큰을 확인한 뒤 저장합니다.")
        self._delete = self._button(bot, "삭제", DELETE_X, row, ACTION_WIDTH, "delete:")
        self._delete.setToolTip_("봇 토큰만 지웁니다. 멤버 ID는 유지합니다.")
        row = form_row_y(bot_height, 1)
        self._row_label(bot, "연결된 봇", row)
        self._identity = self._value(bot, "", row, width=FIELD_WIDTH)
        self._check = self._button(bot, "연결 확인", SAVE_X, row, WIDE_WIDTH, "check:")
        self._check.setToolTip_("메시지를 보내지 않고 저장된 봇 토큰으로 연결을 확인합니다.")
        row = form_row_y(bot_height, 2)
        self._row_label(bot, "테스트", row)
        self._test = self._button(bot, "테스트 알림", SAVE_X, row, WIDE_WIDTH, "test:")
        self._test.setToolTip_(
            "저장한 멤버 ID로 테스트 DM을 한 건 보냅니다. 봇에 chat:write 권한이 필요합니다."
        )
        top -= bot_height + CARD_GAP

        # 앱 토큰: 보관만 하며 DM 발송에는 쓰지 않는다.
        app, app_height, self._app_badge = self._card(document, top, "앱 토큰", APP_ROWS)
        row = form_row_y(app_height, 0)
        caption = self._row_label(app, "토큰", row)
        caption.setToolTip_("Socket Mode 수신용 xapp- 토큰. DM 알림에는 필요 없고 저장만 합니다.")
        self.app_token = self._field(app, row, secure=True)
        self._app_save = self._button(app, "저장", SAVE_X, row, ACTION_WIDTH, "saveAppToken:")
        self._app_delete = self._button(app, "삭제", DELETE_X, row, ACTION_WIDTH, "deleteAppToken:")
        self._app_delete.setToolTip_("앱 토큰만 지웁니다. 봇 토큰은 유지합니다.")
        document.scrollPoint_((0, max(0, height - viewport)))
        self.render()

    def _card(
        self, document: NSView, top: float, title: str, rows: int
    ) -> tuple[NSView, float, SettingsBadge]:
        """위쪽 y가 top인 제목 카드와 제목 줄 오른쪽 상태 배지를 만든다."""
        height = card_height(rows)
        view = NSView.alloc().initWithFrame_(frame(CARD_PADDING, top - height, CARD_WIDTH, height))
        document.addSubview_(view)
        card(view, 0, 0, CARD_WIDTH, height)
        form_dividers(view, height, rows)
        title_y = height - CARD_PADDING - CARD_TITLE
        heading = label(view, title, 0, 0, 300, size=13, bold=True)
        place_label(heading, CARD_PADDING, title_y, 300, CARD_TITLE)
        # 배지는 제목 줄(높이 20) 오른쪽 끝에 붙고 글자 폭만큼만 차지한다.
        badge = SettingsBadge(view, CARD_WIDTH - CARD_PADDING - BADGE_SLOT, title_y, BADGE_SLOT)
        return view, height, badge

    def _row_label(self, parent: NSView, text: str, y: float) -> NSTextField:
        """항목 이름은 왼쪽 칸에서 입력 줄 높이의 가운데에 둔다."""
        name = label(parent, text, 0, 0, FORM_LABEL_WIDTH - 8)
        place_label(name, CARD_PADDING, y, FORM_LABEL_WIDTH - 8, FORM_ROW)
        return name

    def _value(
        self,
        parent: NSView,
        text: str,
        y: float,
        *,
        width: float = CARD_WIDTH - CARD_PADDING - FORM_CONTROL_X,
    ) -> NSTextField:
        """입력 칸 자리에 보조 설명이나 읽기 전용 값을 한 줄로 둔다."""
        value = label(parent, text, 0, 0, width, secondary=True)
        value.cell().setLineBreakMode_(NSLineBreakByTruncatingTail)
        place_label(value, FORM_CONTROL_X, y, width, FORM_ROW)
        return value

    def _button(
        self, parent: NSView, title: str, x: float, y: float, width: float, action: str
    ) -> NSButton:
        """테두리 버튼을 입력 줄 세로 가운데에 둔다."""
        return button(
            parent, title, x, form_control_y(y, BUTTON_HEIGHT), width, self._actions, action
        )

    def _field(self, parent: NSView, y: float, *, secure: bool) -> NSTextField:
        """보안·일반 입력 모두 입력 칸 세로선에서 시작해 줄 가운데에 둔다."""
        field = (
            NSSecureTextField.textFieldWithString_("")
            if secure
            else NSTextField.textFieldWithString_("")
        )
        field.setFont_(NSFont.systemFontOfSize_(13))
        field.setFrame_(
            frame(FORM_CONTROL_X, form_control_y(y, FIELD_HEIGHT), FIELD_WIDTH, FIELD_HEIGHT)
        )
        field.setDelegate_(self._actions)
        parent.addSubview_(field)
        return field

    def render(self) -> None:
        """저장 응답이 다른 카드의 입력이나 저장된 토큰을 되채우지 않게 한다."""
        controller = self.controller
        state = controller.state
        if self._member_generation != controller.member_generation:
            if state is not None:
                self.member.setStringValue_(state.user_id or "")
            self._member_generation = controller.member_generation
        if self._clear_generation != controller.clear_generation:
            self.token.setStringValue_("")
            self._clear_generation = controller.clear_generation
        if self._app_clear_generation != controller.app_clear_generation:
            self.app_token.setStringValue_("")
            self._app_clear_generation = controller.app_clear_generation
        editable = controller.editable
        app_configured = state is not None and state.app_configured
        app_candidate = bool(self.app_token.stringValue())
        self.app_token.setEnabled_(editable)
        self._app_save.setEnabled_(editable and app_candidate)
        self._app_delete.setEnabled_(editable and app_configured and not app_candidate)
        self._app_badge.update(
            *registration_status(
                configured=app_configured,
                error_code=controller.error_code,
                dirty=app_candidate,
                busy=controller.busy
                and controller.operation in {"read", "app-token", "app-delete"},
                error=bool(
                    controller.error and controller.operation in {"read", "app-token", "app-delete"}
                ),
            )
        )
        self.app_token.setPlaceholderString_(
            REPLACE_HINT if app_configured else "xapp-로 시작하는 App-Level Token"
        )
        configured = state is not None and state.configured
        self.member.setEnabled_(editable)
        self.token.setEnabled_(editable)
        self._member_save.setEnabled_(editable and controller.member_dirty)
        candidate = bool(self.token.stringValue())
        self._token_save.setEnabled_(editable and candidate)
        self._check.setEnabled_(editable and configured and not candidate)
        self._test.setEnabled_(controller.can_test and not candidate)
        self._delete.setEnabled_(editable and configured and not candidate)
        self._member_badge.update(
            *registration_status(
                configured=bool(state and state.user_id),
                error_code=controller.error_code,
                dirty=controller.member_dirty,
                busy=controller.busy and controller.operation in {"read", "member"},
                error=bool(controller.error and controller.operation in {"read", "member"}),
            )
        )
        self._token_badge.update(
            *registration_status(
                configured=configured,
                error_code=controller.error_code,
                verification=state.verification if state else "unchecked",
                dirty=candidate,
                busy=controller.busy
                and controller.operation in {"read", "token", "check", "delete", "test"},
                error=bool(
                    controller.error
                    and controller.operation in {"read", "token", "check", "delete", "test"}
                ),
            )
        )
        if not candidate and state and not controller.error:
            identity = " · ".join(
                value for value in (state.identity.workspace, state.identity.bot) if value
            )
        else:
            identity = ""
        # 확인된 봇이 없을 때도 빈 칸 대신 무엇이 나타날지 알려 준다.
        self._identity.setStringValue_(identity or "연결 확인 후 표시")
        self._identity.setToolTip_(identity or None)
        self.token.setPlaceholderString_(
            REPLACE_HINT if configured else "xoxb-로 시작하는 Bot User OAuth Token"
        )
