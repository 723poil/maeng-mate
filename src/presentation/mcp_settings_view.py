"""앱 내 MCP 주소·사용 선택과 조회 토큰을 관리하는 기본 화면이다."""

import time
from dataclasses import dataclass
from datetime import datetime
from math import inf

from AppKit import (
    NSButton,
    NSLineBreakByTruncatingTail,
    NSPasteboard,
    NSPasteboardTypeString,
    NSPopUpButton,
    NSScrollView,
    NSSecureTextField,
    NSSwitch,
    NSTextField,
    NSView,
)
from Foundation import NSNotification, NSObject

from src.application.ai_skills import SkillTarget, SkillVersions, UnversionedSkill
from src.domain.mcp_access import McpAccessToken
from src.domain.work_dates import SEOUL
from src.presentation.mcp_settings_controller import McpSettingsController
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
from src.presentation.settings_badge import SettingsBadge

PERIODS: tuple[tuple[str, int | None], ...] = (
    ("1일", 1),
    ("7일", 7),
    ("30일", 30),
    ("90일", 90),
    ("만료 없음", None),
)
SKILL_TARGETS: tuple[tuple[str, SkillTarget], ...] = (
    ("Codex", "codex"),
    ("Claude Code", "claude"),
    ("둘 다", "both"),
)
# 페이지 문서 폭과 맨 위 카드 위 여백이다.
PAGE_WIDTH = 584.0
PAGE_TOP = 8.0
# 컨트롤 높이: 입력칸·스위치 22, 팝업·테두리 버튼 26, 배지 20.
FIELD_HEIGHT = 22.0
POPUP_HEIGHT = 26.0
BADGE_HEIGHT = 20.0
SWITCH_WIDTH = 40.0
POPUP_WIDTH = 140.0
STATUS_WIDTH = 120.0
# 오른쪽 끝 버튼 폭이다. 복사 버튼 문구는 제어기 안내(‘토큰 복사’를 누르세요)와 같아야 한다.
COPY_WIDTH = 80.0
INSTALL_WIDTH = 104.0
ISSUE_WIDTH = 72.0
REVOKE_WIDTH = 64.0
# 입력칸 오른쪽 끝은 복사 버튼의 보이는 테두리 왼쪽에서 8pt 띄운다.
FIELD_WIDTH = CARD_WIDTH - CARD_PADDING - COPY_WIDTH + 2 * BUTTON_BEZEL_INSET - 8 - FORM_CONTROL_X
# 토큰 줄의 칸: 이름(최대 60자라 넓게) · 만료일 · 상태 배지 · 폐기 버튼.
TOKEN_NAME_WIDTH = 200.0
EXPIRY_X = CARD_PADDING + TOKEN_NAME_WIDTH + 8
EXPIRY_WIDTH = 112.0
TOKEN_BADGE_X = EXPIRY_X + EXPIRY_WIDTH + 8
TOKEN_BADGE_WIDTH = 72.0


def _date(value: float) -> str:
    """토큰의 UTC 시점을 한국 날짜·시각으로 표시한다."""
    return datetime.fromtimestamp(value, SEOUL).strftime("%Y.%m.%d %H:%M")


def _version_label(version: str | UnversionedSkill, versions: SkillVersions) -> str:
    """번호를 알 수 없는 설치 기록은 이유에 맞는 짧은 문구로 표시한다."""
    if isinstance(version, str):
        return f"{version} (업데이트 가능)" if versions.update_available(version) else version
    return {
        UnversionedSkill.MISSING: "미설치",
        UnversionedSkill.LEGACY: "버전 미기록",
        UnversionedSkill.UNKNOWN: "확인 불가",
    }[version]


def _last_used(value: float | None) -> str:
    """사용한 적이 없는 토큰은 날짜 대신 안내 문구를 표시한다."""
    return f"최근 사용: {_date(value)}" if value is not None else "사용 기록 없음"


class McpSettingsActions(NSObject):
    """MCP 입력을 다른 설정 폼과 섞지 않고 연결한다."""

    owner: "McpSettingsView"

    def enabled_(self, sender: NSSwitch) -> None:
        """사용 변경은 명시적 스위치 입력에서만 저장한다."""
        self.owner.controller.set_enabled(sender.state() == 1)
        self.owner.render()

    def issue_(self, sender: object) -> None:
        """현재 이름·선택 기간으로 한 번만 발급한다."""
        selected = self.owner.period.indexOfSelectedItem()
        if 0 <= selected < len(PERIODS):
            self.owner.controller.issue(self.owner.name.stringValue(), PERIODS[selected][1])
        self.owner.render()

    def revoke_(self, sender: object) -> None:
        """버튼의 목록 인덱스를 현재 토큰 ID로 바꾼다."""
        if isinstance(sender, NSButton) and self.owner.controller.state is not None:
            items = self.owner.controller.state.snapshot.tokens
            index = sender.tag()
            if 0 <= index < len(items):
                self.owner.controller.revoke(items[index].id)
        self.owner.render()

    def copyEndpoint_(self, sender: object) -> None:
        """사용자가 누른 경우에만 접속 주소를 복사한다."""
        if self.owner.controller.state is not None:
            self.owner.copy(self.owner.controller.state.endpoint)

    def copyToken_(self, sender: object) -> None:
        """발급 직후 메모리에 남은 값만 복사하며 원문을 다시 조회하지 않는다."""
        token = self.owner.controller.one_time_token
        if token is not None:
            self.owner.copy(token.value)

    def controlTextDidChange_(self, notification: NSNotification) -> None:
        """토큰 이름 입력은 일반·Notion 설정 초안으로 처리하지 않는다."""
        self.owner.render()

    def installSkill_(self, sender: object) -> None:
        """선택한 AI에만 번들 지침을 설치한다."""
        index = self.owner.skill_target.indexOfSelectedItem()
        if 0 <= index < len(SKILL_TARGETS):
            self.owner.controller.install_skill(SKILL_TARGETS[index][1])
        self.owner.render()


@dataclass
class _TokenControls:
    """목록 구조가 그대로일 때 갱신할 배지와 폐기 버튼이다."""

    badge: SettingsBadge
    revoke: NSButton
    status: str | None = None


class McpSettingsView:
    """서버·발급·토큰 목록·스킬 카드를 위에서부터 쌓고 페이지 전체를 스크롤한다."""

    def __init__(
        self, page: NSScrollView, viewport: float, controller: McpSettingsController
    ) -> None:
        """네 카드를 만들고, 토큰 수에 따라 늘어나는 목록 카드에 맞춰 페이지 높이를 정한다."""
        self.controller = controller
        self._secret_generation = -1
        self._issue_generation = 0
        self._list_key: tuple[bool, tuple[McpAccessToken, ...]] | None = None
        self._token_controls: list[_TokenControls] = []
        self._list_editable: bool | None = None
        self._checked_at = inf
        self._next_expiry = inf
        self._actions = McpSettingsActions.alloc().init()
        self._actions.owner = self
        self._page = page
        self._viewport = viewport
        self._document = NSView.alloc().initWithFrame_(frame(0, 0, PAGE_WIDTH, viewport))
        page.setDocumentView_(self._document)

        # 서버 카드: 사용 상태와 스위치, 접속 주소, 마지막 사용 시각.
        self._server = self._section()
        height = self._draw_card(self._server, "MCP 서버", 3)
        row = form_row_y(height, 0)
        self._row_label(self._server, "사용", row)
        self._status = SettingsBadge(
            self._server,
            FORM_CONTROL_X,
            form_control_y(row, BADGE_HEIGHT),
            STATUS_WIDTH,
            align="left",
        )
        self.enabled = NSSwitch.alloc().initWithFrame_(
            frame(
                CARD_WIDTH - CARD_PADDING - SWITCH_WIDTH,
                form_control_y(row, FIELD_HEIGHT),
                SWITCH_WIDTH,
                FIELD_HEIGHT,
            )
        )
        self.enabled.setTarget_(self._actions)
        self.enabled.setAction_("enabled:")
        self.enabled.setAccessibilityLabel_("MCP 사용")
        self._server.addSubview_(self.enabled)
        row = form_row_y(height, 1)
        self._row_label(self._server, "주소", row)
        self.endpoint = NSTextField.textFieldWithString_("")
        self._place_field(self._server, self.endpoint, row)
        self.endpoint.setEditable_(False)
        self.endpoint.setSelectable_(True)
        self._end_button(self._server, "주소 복사", COPY_WIDTH, row, "copyEndpoint:")
        row = form_row_y(height, 2)
        self._row_label(self._server, "최근 사용", row)
        self._recent = self._value(self._server, row)

        # 발급 카드: 이름·기간을 고른 뒤 발급하고, 원문은 이 창에서 한 번만 복사한다.
        self._issuing = self._section()
        height = self._draw_card(self._issuing, "토큰 발급", 3)
        row = form_row_y(height, 0)
        self._row_label(self._issuing, "이름", row)
        self.name = NSTextField.textFieldWithString_("")
        self._place_field(self._issuing, self.name, row)
        self.name.setPlaceholderString_("예: 업무용")
        self.name.setDelegate_(self._actions)
        row = form_row_y(height, 1)
        self._row_label(self._issuing, "유효 기간", row)
        self.period = self._popup(self._issuing, row, [title for title, _ in PERIODS], 2)
        self._issue = self._end_button(self._issuing, "발급", ISSUE_WIDTH, row, "issue:")
        row = form_row_y(height, 2)
        self._row_label(self._issuing, "새 토큰", row)
        self.issued = NSSecureTextField.textFieldWithString_("")
        self._place_field(self._issuing, self.issued, row)
        self.issued.setEditable_(False)
        self.issued.setSelectable_(True)
        self.issued.setPlaceholderString_("발급 후 한 번만 복사할 수 있습니다")
        self._copy = self._end_button(self._issuing, "토큰 복사", COPY_WIDTH, row, "copyToken:")

        # 토큰 목록 카드는 render에서 목록이 바뀔 때마다 다시 그린다.
        self._tokens = self._section()

        # 스킬 카드는 한 번 설치하면 되므로 늘어나는 목록 아래에 둔다.
        self._skill = self._section()
        height = self._draw_card(self._skill, "AI 스킬", 2)
        row = form_row_y(height, 0)
        self._row_label(self._skill, "설치할 앱", row)
        self.skill_target = self._popup(
            self._skill, row, [name for name, _ in SKILL_TARGETS], len(SKILL_TARGETS) - 1
        )
        self._install_skill = self._end_button(
            self._skill, "설치·업데이트", INSTALL_WIDTH, row, "installSkill:"
        )
        self._install_skill.setToolTip_(
            "선택한 앱에 업무 조회용 스킬을 설치합니다. MCP 연결은 해당 앱에서 설정하세요."
        )
        row = form_row_y(height, 1)
        self._row_label(self._skill, "설치 버전", row)
        self._versions = self._value(self._skill, row)
        self.render()

    def _section(self) -> NSView:
        """카드 하나를 담는 뷰다. 위치는 _layout에서 위쪽부터 정한다."""
        view = NSView.alloc().initWithFrame_(frame(0, 0, CARD_WIDTH, 0))
        self._document.addSubview_(view)
        return view

    def _draw_card(self, view: NSView, title: str, rows: int) -> float:
        """카드 배경·줄 구분선·제목을 그리고 카드 높이를 돌려준다."""
        height = card_height(rows)
        view.setFrameSize_((CARD_WIDTH, height))
        card(view, 0, 0, CARD_WIDTH, height)
        form_dividers(view, height, rows)
        caption = label(view, title, 0, 0, 300, size=13, bold=True)
        place_label(caption, CARD_PADDING, height - CARD_PADDING - CARD_TITLE, 300, CARD_TITLE)
        return height

    def _row_label(self, parent: NSView, text: str, y: float) -> None:
        """항목 이름은 왼쪽 칸에서 입력 줄 높이의 가운데에 둔다."""
        name = label(parent, text, 0, 0, FORM_LABEL_WIDTH - 8)
        place_label(name, CARD_PADDING, y, FORM_LABEL_WIDTH - 8, FORM_ROW)

    def _value(self, parent: NSView, y: float) -> NSTextField:
        """읽기 전용 값은 입력 칸 세로선에서 시작하고 길면 끝을 줄인다."""
        width = CARD_WIDTH - CARD_PADDING - FORM_CONTROL_X
        value = label(parent, "", 0, 0, width)
        value.cell().setLineBreakMode_(NSLineBreakByTruncatingTail)
        place_label(value, FORM_CONTROL_X, y, width, FORM_ROW)
        return value

    def _place_field(self, parent: NSView, field: NSTextField, y: float) -> None:
        """입력칸은 항목 칸 오른쪽에서 시작해 오른쪽 버튼 앞까지 기본 높이로 둔다."""
        field.setFrame_(
            frame(FORM_CONTROL_X, form_control_y(y, FIELD_HEIGHT), FIELD_WIDTH, FIELD_HEIGHT)
        )
        parent.addSubview_(field)

    def _popup(self, parent: NSView, y: float, titles: list[str], selected: int) -> NSPopUpButton:
        """팝업 테두리가 입력 세로선에 맞도록 3pt 앞에서 시작한다."""
        popup = NSPopUpButton.alloc().initWithFrame_pullsDown_(
            frame(FORM_CONTROL_X - 3, form_control_y(y, POPUP_HEIGHT), POPUP_WIDTH, POPUP_HEIGHT),
            False,
        )
        popup.addItemsWithTitles_(titles)
        popup.selectItemAtIndex_(selected)
        parent.addSubview_(popup)
        return popup

    def _end_button(
        self, parent: NSView, title: str, width: float, y: float, action: str
    ) -> NSButton:
        """보이는 테두리가 카드 안쪽 오른쪽 여백에 닿도록 프레임을 테두리 여백만큼 민다."""
        return button(
            parent,
            title,
            CARD_WIDTH - CARD_PADDING - width + BUTTON_BEZEL_INSET,
            form_control_y(y, POPUP_HEIGHT),
            width,
            self._actions,
            action,
        )

    def _layout(self) -> None:
        """카드 높이 합에 맞춰 페이지 높이를 바꾸고 보던 위치를 위쪽 기준으로 유지한다."""
        sections = (self._server, self._issuing, self._tokens, self._skill)
        heights = [view.frame()[1][1] for view in sections]
        content = PAGE_TOP + sum(heights) + CARD_GAP * (len(sections) - 1) + CARD_PADDING
        old_height = self._document.frame()[1][1]
        old_origin = self._page.contentView().bounds()[0][1]
        height = max(self._viewport, content)
        self._document.setFrameSize_((PAGE_WIDTH, height))
        top = height - PAGE_TOP
        for view, section_height in zip(sections, heights, strict=True):
            view.setFrame_(frame(CARD_PADDING, top - section_height, CARD_WIDTH, section_height))
            top -= section_height + CARD_GAP
        # 문서 원점은 아래쪽이므로 늘어난 높이만큼 원점을 올려야 같은 위쪽 내용이 보인다.
        self._document.scrollPoint_(
            (0, max(0, min(height - self._viewport, old_origin + height - old_height)))
        )

    @property
    def is_editing(self) -> bool:
        """토큰 이름이 남아 있거나 편집 중이면 재시작에서 초안을 보호한다."""
        return bool(self.name.stringValue()) or self.name.currentEditor() is not None

    def copy(self, value: str) -> None:
        """명시적 복사 버튼에 한해서 클립보드를 사용한다."""
        board = NSPasteboard.generalPasteboard()
        board.clearContents()
        board.setString_forType_(value, NSPasteboardTypeString)

    def render(self) -> None:
        """새 원문을 한 번만 채우고 목록 갱신에서 입력을 보존한다."""
        controller = self.controller
        state = controller.state
        editable = controller.editable
        self.skill_target.setEnabled_(controller.can_install_skill)
        self._install_skill.setEnabled_(controller.can_install_skill)
        versions = controller.skill_versions
        # 줄 이름이 ‘설치 버전’이므로 값에는 앱에 든 최신 버전과 앱별 설치 버전만 적는다.
        text = (
            f"최신 {versions.bundled}  ·  Codex {_version_label(versions.codex, versions)}"
            f"  ·  Claude Code {_version_label(versions.claude, versions)}"
            if versions
            else "확인 중…"
            if controller.busy
            else "확인할 수 없음"
        )
        self._versions.setStringValue_(text)
        self._versions.setToolTip_(text)
        self.enabled.setEnabled_(editable)
        self.name.setEnabled_(editable)
        self.period.setEnabled_(editable)
        self._issue.setEnabled_(editable and bool(self.name.stringValue().strip()))
        if state is not None:
            self.enabled.setState_(
                int(
                    controller.pending_enabled
                    if controller.pending_enabled is not None
                    else state.snapshot.enabled
                )
            )
            self.endpoint.setStringValue_(state.endpoint)
            active = state.ready and state.snapshot.enabled
            if controller.pending_enabled is not None:
                self._status.update("변경 중", "blue")
            else:
                self._status.update(
                    "실행 중" if active else "중지됨", "green" if active else "neutral"
                )
        else:
            self._status.update(
                "확인 불가" if controller.error else "불러오는 중",
                "orange" if controller.error else "neutral",
            )
        if controller.error:
            self._status.update("확인 불가", "orange")
        if self._secret_generation != controller.secret_generation:
            self.issued.setStringValue_(
                controller.one_time_token.value if controller.one_time_token else ""
            )
            self._secret_generation = controller.secret_generation
        if self._issue_generation != controller.issue_generation:
            self.name.setStringValue_("")
            self._issue_generation = controller.issue_generation
        self._copy.setEnabled_(controller.one_time_token is not None)
        self._render_tokens()

    def _render_tokens(self) -> None:
        """상태가 바뀔 때만 행을 갱신하고 같은 목록의 스크롤은 유지한다."""
        controller = self.controller
        state = controller.state
        now = time.time()
        items = state.snapshot.tokens if state else ()
        key = (state is not None, items)
        data_changed = key != self._list_key
        self._list_key = key
        if data_changed:
            self._build_token_rows(items, loaded=state is not None)
            if state is not None:
                latest = max(
                    (item.last_used_at for item in items if item.last_used_at is not None),
                    default=None,
                )
                self._recent.setStringValue_(_date(latest) if latest is not None else "기록 없음")
        # 같은 구간에서는 시간이 지나도 상태가 같다. 시계가 뒤로 이동하면 다시 판정한다.
        check_status = data_changed or not self._checked_at <= now < self._next_expiry
        editable = controller.editable
        if not check_status and editable == self._list_editable:
            return
        if check_status:
            self._checked_at, self._next_expiry = now, inf
        for item, controls in zip(items, self._token_controls, strict=True):
            if check_status:
                status = item.status_at(now)
                if status == "active" and item.expires_at is not None:
                    self._next_expiry = min(self._next_expiry, item.expires_at)
                if status != controls.status:
                    controls.badge.update(
                        {"active": "유효", "expired": "만료", "revoked": "폐기됨"}[status],
                        {"active": "green", "expired": "orange", "revoked": "neutral"}[status],
                    )
                    controls.status = status
            if data_changed or editable != self._list_editable:
                controls.revoke.setEnabled_(editable and item.revoked_at is None)
        self._list_editable = editable

    def _build_token_rows(self, items: tuple[McpAccessToken, ...], *, loaded: bool) -> None:
        """토큰 카드를 새 목록으로 다시 그리고 늘어난 높이에 맞춰 페이지를 다시 배치한다."""
        self._tokens.setSubviews_([])
        self._token_controls.clear()
        # 빈 목록도 안내 한 줄을 두어 카드 모양을 유지한다.
        height = self._draw_card(self._tokens, "발급한 토큰", max(1, len(items)))
        if not items:
            empty = label(
                self._tokens,
                "발급한 토큰이 없습니다" if loaded else "불러오는 중…",
                0,
                0,
                300,
                secondary=True,
            )
            place_label(empty, CARD_PADDING, form_row_y(height, 0), 300, FORM_ROW)
        for index, item in enumerate(items):
            y = form_row_y(height, index)
            name = label(self._tokens, item.name, 0, 0, TOKEN_NAME_WIDTH)
            # 이름은 최대 60자라 칸을 넘으면 끝을 줄이고 전체는 도움말로 보여 준다.
            name.cell().setLineBreakMode_(NSLineBreakByTruncatingTail)
            place_label(name, CARD_PADDING, y, TOKEN_NAME_WIDTH, FORM_ROW)
            name.setToolTip_(item.name + "\n" + _last_used(item.last_used_at))
            expiry = label(
                self._tokens,
                "만료 " + _date(item.expires_at)[:10]
                if item.expires_at is not None
                else "만료 없음",
                0,
                0,
                EXPIRY_WIDTH,
                secondary=True,
            )
            place_label(expiry, EXPIRY_X, y, EXPIRY_WIDTH, FORM_ROW)
            expiry.setToolTip_(
                "만료: " + _date(item.expires_at) if item.expires_at is not None else "만료 없음"
            )
            badge = SettingsBadge(
                self._tokens,
                TOKEN_BADGE_X,
                form_control_y(y, BADGE_HEIGHT),
                TOKEN_BADGE_WIDTH,
                align="left",
            )
            revoke = self._end_button(self._tokens, "폐기", REVOKE_WIDTH, y, "revoke:")
            revoke.setTag_(index)
            self._token_controls.append(_TokenControls(badge, revoke))
        self._layout()
