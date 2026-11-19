"""Notion 계정과 조회 옵션을 편집하는 기본 macOS 폼이다."""

from AppKit import NSFont, NSPopUpButton, NSScrollView, NSTextField, NSView
from Foundation import NSObject

from src.application.runtime_settings import parse_runtime_settings
from src.domain.runtime_settings import RuntimeSettings
from src.presentation.native_views import card, frame, label
from src.presentation.runtime_settings_controller import RuntimeSettingsController


class RuntimeSettingsForm:
    """페이지 전환·재열기에서 입력 초안을 유지하는 두 개의 폼이다."""

    def __init__(
        self,
        notion_page: NSScrollView,
        viewport: float,
        controller: RuntimeSettingsController,
        actions: NSObject,
    ) -> None:
        """같은 카드 여백과 시스템 입력 컨트롤로 일반·연동을 구분한다."""
        self.controller = controller
        self._generation = 0
        height = max(370.0, viewport)
        notion = NSView.alloc().initWithFrame_(frame(0, 0, 584, height))
        notion_page.setDocumentView_(notion)
        card(notion, 16, height - 90, 552, 82)
        label(notion, "내 Notion ID", 28, height - 42, 104, size=13, bold=True)
        self.user_id = self._input(notion, 144, height - 46, 412, actions)
        label(notion, "목록 범위", 28, height - 76, 104, size=13, bold=True)
        self.policy = NSPopUpButton.alloc().initWithFrame_pullsDown_(
            frame(144, height - 80, 412, 28), False
        )
        self.policy.addItemsWithTitles_(["담당 업무", "담당 업무와 참여 일정"])
        self.policy.setToolTip_(
            "사람 칼럼을 연결한 표에서 내가 참여한 일정도 목록에 표시할지 정합니다."
        )
        self.policy.setFont_(NSFont.systemFontOfSize_(13))
        popup_height = min(28.0, self.policy.fittingSize()[1])
        self.policy.setFrame_(frame(144, height - 80 + (28 - popup_height) / 2, 412, popup_height))
        self.policy.setTarget_(actions)
        self.policy.setAction_("runtimeEdited:")
        notion.addSubview_(self.policy)
        for document in (notion,):
            document.scrollPoint_((0, max(0, height - viewport)))
        self.render()

    def _input(
        self, parent: NSView, x: float, y: float, width: float, actions: NSObject
    ) -> NSTextField:
        """입력 중인 문자열을 보존하고 매 글자 변경을 초안으로 알린다."""
        field = NSTextField.textFieldWithString_("")
        field.setFont_(NSFont.systemFontOfSize_(13))
        # 기본 입력 셀은 높은 프레임 안에서 위로 붙으므로 권장 높이로 줄여 행 중앙에 둔다.
        input_height = min(28.0, field.fittingSize()[1])
        field.setFrame_(frame(x, y + (28 - input_height) / 2, width, input_height))
        field.setDelegate_(actions)
        parent.addSubview_(field)
        return field

    def draft(self) -> RuntimeSettings:
        """입력 범위와 버전을 검증한 뒤 저장 후보를 만든다."""
        state = self.controller.state
        if state is None:
            raise ValueError("먼저 설정을 불러오세요.")
        return parse_runtime_settings(
            {
                "revision": state.settings.revision,
                "general": {
                    "relevancePolicy": "assignment"
                    if self.policy.indexOfSelectedItem() == 0
                    else "participation",
                    # 예약이 없는 자료의 내부 기본값은 화면 편집 없이 보존한다.
                    "snapshotMaxAgeSeconds": state.settings.general.snapshot_max_age_seconds,
                },
                "providers": {
                    "notion": {"userId": self.user_id.stringValue().strip() or None},
                    "slack": {"userId": state.settings.slack.user_id},
                },
            }
        )

    def render(self) -> None:
        """새 조회·저장 성공에서만 값을 채워 입력 중인 초안을 덮지 않는다."""
        controller = self.controller
        if controller.state is not None and self._generation != controller.generation:
            value = controller.state.settings
            self.user_id.setStringValue_(value.notion.user_id or "")
            self.policy.selectItemAtIndex_(
                0 if value.general.relevance_policy == "assignment" else 1
            )
            self._generation = controller.generation
        for control in (self.user_id, self.policy):
            control.setEnabled_(controller.editable)
