"""개인 수신자와 봇 토큰을 구분한 컴팩트한 Slack 설정 화면이다."""

from AppKit import NSFont, NSScrollView, NSSecureTextField, NSTextField, NSView
from Foundation import NSNotification, NSObject

from src.application.credentials import CredentialError, validated_secret
from src.application.slack_settings import validated_app_token
from src.presentation.native_views import button, card, frame, label
from src.presentation.registration_status import registration_status
from src.presentation.settings_badge import SettingsBadge
from src.presentation.slack_settings_controller import SlackSettingsController


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
    """시스템 입력 높이·정렬과 기존 설정 카드 간격을 재사용한다."""

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
        height = max(416.0, viewport)
        document = NSView.alloc().initWithFrame_(frame(0, 0, 584, height))
        page.setDocumentView_(document)
        card(document, 16, height - 96, 552, 88)
        label(document, "개인 DM", 28, height - 40, 200, size=13, bold=True)
        self._member_badge = SettingsBadge(document, 456, height - 40, 100)
        caption = label(document, "멤버 ID", 28, height - 78, 84, size=12, secondary=True)
        caption.setToolTip_("Slack 본인 프로필 → 더보기 → 멤버 ID 복사")
        self.member = self._field(document, 120, height - 82, 336, secure=False)
        self.member.setPlaceholderString_("본인 멤버 ID")
        self._member_save = button(
            document, "저장", 464, height - 81, 92, self._actions, "saveMember:"
        )
        card(document, 16, height - 260, 552, 152)
        label(document, "봇 토큰", 28, height - 140, 200, size=13, bold=True)
        self._token_badge = SettingsBadge(document, 456, height - 140, 100)
        self.token = self._field(document, 28, height - 178, 528, secure=True)
        self.token.setPlaceholderString_("Bot User OAuth Token")
        self._token_save = button(
            document, "확인 후 저장", 28, height - 212, 112, self._actions, "saveToken:"
        )
        self._check = button(document, "연결 확인", 148, height - 212, 100, self._actions, "check:")
        self._check.setToolTip_("메시지를 보내지 않고 저장된 봇 토큰으로 연결을 확인합니다.")
        self._test = button(document, "테스트 알림", 256, height - 212, 112, self._actions, "test:")
        self._test.setToolTip_(
            "저장한 멤버 ID로 테스트 DM을 한 건 보냅니다. 봇에 chat:write 권한이 필요합니다."
        )
        self._delete = button(document, "삭제", 486, height - 212, 70, self._actions, "delete:")
        self._identity = label(document, "", 28, height - 244, 528, size=12, secondary=True)
        card(document, 16, height - 400, 552, 128)
        caption = label(document, "앱 토큰", 28, height - 304, 200, size=13, bold=True)
        caption.setToolTip_("Socket Mode 수신용 xapp- 토큰. 저장만 하며 연결은 시작하지 않습니다.")
        self._app_badge = SettingsBadge(document, 456, height - 304, 100)
        self.app_token = self._field(document, 28, height - 342, 528, secure=True)
        self.app_token.setPlaceholderString_("App-Level Token · xapp-…")
        self._app_save = button(
            document, "저장", 28, height - 376, 92, self._actions, "saveAppToken:"
        )
        self._app_delete = button(
            document, "삭제", 486, height - 376, 70, self._actions, "deleteAppToken:"
        )
        document.scrollPoint_((0, max(0, height - viewport)))
        self.render()

    def _field(
        self, parent: NSView, x: float, y: float, width: float, *, secure: bool
    ) -> NSTextField:
        """보안·일반 입력 모두 기본 글꼴의 실제 높이로 행 중앙에 놓는다."""
        field = (
            NSSecureTextField.textFieldWithString_("")
            if secure
            else NSTextField.textFieldWithString_("")
        )
        field.setFont_(NSFont.systemFontOfSize_(13))
        height = min(28.0, field.fittingSize()[1])
        field.setFrame_(frame(x, y + (28 - height) / 2, width, height))
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
            "저장된 토큰을 바꾸려면 입력하세요" if app_configured else "App-Level Token · xapp-…"
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
        self._identity.setStringValue_(identity)
        self._identity.setToolTip_(identity or None)
        from AppKit import NSLineBreakByTruncatingTail

        self._identity.cell().setLineBreakMode_(NSLineBreakByTruncatingTail)
        self.token.setPlaceholderString_(
            "저장된 토큰을 바꾸려면 입력하세요" if configured else "Bot User OAuth Token"
        )
