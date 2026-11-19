"""앱 내 MCP 주소·사용 선택과 조회 토큰을 관리하는 기본 화면이다."""

import time
from dataclasses import dataclass
from datetime import datetime
from math import inf

from AppKit import (
    NSButton,
    NSFont,
    NSNoBorder,
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
from src.presentation.native_views import button, card, frame, label, place_label
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
    """컴팩트한 입력과 스크롤 목록에서 여러 토큰을 관리한다."""

    def __init__(
        self, page: NSScrollView, viewport: float, controller: McpSettingsController
    ) -> None:
        """서버 상태·발급·목록을 한 페이지에 배치한다."""
        self.controller = controller
        self._secret_generation = -1
        self._issue_generation = 0
        self._list_key: tuple[bool, tuple[McpAccessToken, ...]] | None = None
        self._token_controls: list[_TokenControls] = []
        self._list_editable: bool | None = None
        self._checked_at = inf
        self._next_expiry = inf
        self._list_ids: tuple[str, ...] = ()
        self._actions = McpSettingsActions.alloc().init()
        self._actions.owner = self
        height = max(464.0, viewport)
        document = NSView.alloc().initWithFrame_(frame(0, 0, 584, height))
        page.setDocumentView_(document)
        card(document, 16, height - 148, 552, 140)
        label(document, "MCP 서버", 28, height - 40, 170, size=13, bold=True)
        self._status = SettingsBadge(document, 390, height - 40, 100)
        self.enabled = NSSwitch.alloc().initWithFrame_(frame(512, height - 43, 44, 26))
        self.enabled.setTarget_(self._actions)
        self.enabled.setAction_("enabled:")
        self.enabled.setAccessibilityLabel_("MCP 사용")
        document.addSubview_(self.enabled)
        self.endpoint = self._field(document, 28, height - 78, 440)
        self.endpoint.setEditable_(False)
        self.endpoint.setSelectable_(True)
        button(document, "주소 복사", 476, height - 77, 80, self._actions, "copyEndpoint:")
        self._recent = label(document, "", 28, height - 107, 236, size=11, secondary=True)
        self.skill_target = NSPopUpButton.alloc().initWithFrame_pullsDown_(
            frame(276, height - 112, 160, 28), False
        )
        self.skill_target.addItemsWithTitles_([name for name, _ in SKILL_TARGETS])
        self.skill_target.selectItemAtIndex_(2)
        document.addSubview_(self.skill_target)
        self._install_skill = button(
            document, "설치 / 업데이트", 438, height - 111, 118, self._actions, "installSkill:"
        )
        self._install_skill.setToolTip_(
            "선택한 앱에 업무 조회용 스킬을 설치합니다. MCP 연결은 해당 앱에서 설정하세요."
        )
        self._versions = label(document, "", 28, height - 137, 528, size=11, secondary=True)
        card(document, 16, height - 284, 552, 124)
        label(document, "토큰 발급", 28, height - 190, 528, size=13, bold=True)
        self.name = self._field(document, 28, height - 230, 220)
        self.name.setPlaceholderString_("토큰 이름 (예: 업무용)")
        self.name.setDelegate_(self._actions)
        self.period = NSPopUpButton.alloc().initWithFrame_pullsDown_(
            frame(256, height - 230, 160, 28), False
        )
        self.period.addItemsWithTitles_([title for title, _ in PERIODS])
        self.period.selectItemAtIndex_(2)
        document.addSubview_(self.period)
        self._issue = button(document, "발급", 434, height - 229, 122, self._actions, "issue:")
        self.issued = NSSecureTextField.textFieldWithString_("")
        self.issued.setFont_(NSFont.systemFontOfSize_(13))
        place_label(self.issued, 28, height - 264, 440, 26)
        self.issued.setEditable_(False)
        self.issued.setSelectable_(True)
        self.issued.setPlaceholderString_("토큰을 발급하면 여기서 복사할 수 있습니다")
        document.addSubview_(self.issued)
        self._copy = button(
            document, "토큰 복사", 476, height - 264, 80, self._actions, "copyToken:"
        )
        label(document, "등록된 토큰", 28, height - 314, 528, size=13, bold=True)
        card(document, 16, 8, 552, height - 334)
        self._list_height = height - 342
        self._scroll = NSScrollView.alloc().initWithFrame_(frame(28, 12, 528, self._list_height))
        self._scroll.setHasVerticalScroller_(True)
        self._scroll.setAutohidesScrollers_(True)
        self._scroll.setDrawsBackground_(False)
        self._scroll.setBorderType_(NSNoBorder)
        document.addSubview_(self._scroll)
        document.scrollPoint_((0, max(0, height - viewport)))
        self.render()

    def _field(self, parent: NSView, x: float, y: float, width: float) -> NSTextField:
        """기본 입력 높이로 줄여 같은 행의 중앙에 배치한다."""
        field = NSTextField.textFieldWithString_("")
        field.setFont_(NSFont.systemFontOfSize_(13))
        height = field.fittingSize()[1]
        field.setFrame_(frame(x, y + (28 - height) / 2, width, height))
        parent.addSubview_(field)
        return field

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
        self._versions.setStringValue_(
            f"동봉 스킬 {versions.bundled}    ·    Codex {_version_label(versions.codex, versions)}"
            f"    ·    Claude Code {_version_label(versions.claude, versions)}"
            if versions
            else "스킬 버전 확인 중…"
            if controller.busy
            else "스킬 버전 확인 불가"
        )
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
                self._recent.setStringValue_(_last_used(latest))
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
        """새 목록의 표시 정보를 배치하고 같은 토큰 순서라면 스크롤 위치를 보존한다."""
        old = self._scroll.documentView()
        origin = self._scroll.contentView().bounds()[0][1] if old is not None else 0
        ids = tuple(item.id for item in items)
        same_ids = ids == self._list_ids and old is not None
        self._list_ids = ids
        self._token_controls.clear()
        height = max(self._list_height, len(items) * 34)
        document = NSView.alloc().initWithFrame_(frame(0, 0, 528, height))
        if not items:
            label(
                document,
                "발급된 토큰 없음" if loaded else "상태를 불러오는 중…",
                0,
                height - 25,
                528,
                size=12,
                secondary=True,
            )
        for index, item in enumerate(items):
            y = height - (index + 1) * 34
            name = label(document, item.name, 0, y + 7, 170, size=12)
            name.setToolTip_(item.name + "\n" + _last_used(item.last_used_at))
            expiry = label(
                document,
                "만료 " + _date(item.expires_at)[:10]
                if item.expires_at is not None
                else "만료 없음",
                180,
                y + 8,
                124,
                size=11,
                secondary=True,
            )
            expiry.setToolTip_(
                "만료: " + _date(item.expires_at) if item.expires_at is not None else "만료 없음"
            )
            badge = SettingsBadge(document, 310, y + 7, 100)
            revoke = button(document, "폐기", 450, y + 4, 78, self._actions, "revoke:")
            revoke.setTag_(index)
            self._token_controls.append(_TokenControls(badge, revoke))
        self._scroll.setDocumentView_(document)
        document.scrollPoint_((0, origin if same_ids else max(0, height - self._list_height)))
