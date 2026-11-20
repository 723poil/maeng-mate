"""알림 설정을 채널별 스위치 행과 복수 시각 선택으로 구성한다."""

from AppKit import (
    NSBox,
    NSBoxSeparator,
    NSScrollView,
    NSSwitch,
    NSView,
)
from Foundation import NSObject

from src.application.notifications import NotificationChannel
from src.domain.category_event_alerts import EVENT_ALERT_TRIGGERS
from src.domain.notification_timing import departure_minutes
from src.domain.source_display import SourceCategory
from src.presentation.category_sheet_controls import field
from src.presentation.native_views import button, card, frame, label, place_label

ROW_HEIGHT = 34
GROUP_GAP = 12
HEADING_HEIGHT = 22


def category_alert_choices(category: SourceCategory) -> dict[str, tuple[str, tuple[str, ...]]]:
    """소스 연결 전에도 사용자가 등록한 규칙의 이름과 구독 키를 표시한다."""
    config = category.configuration
    if config is None:
        return {}
    choices = {
        event.trigger: (event.name, EVENT_ALERT_TRIGGERS[event.trigger].kinds)
        for event in config.event_alerts or ()
    }
    for entry in config.alerts:
        rule = entry.rule
        choices[rule.notification_kind] = (rule.name, (rule.notification_kind,))
    return choices


class NotificationPreferences:
    """채널 수에 맞춰 내용 높이를 계산하는 알림 설정 본문이다."""

    def __init__(
        self,
        page: NSScrollView,
        viewport: float,
        channels: tuple[NotificationChannel, ...],
        actions: NSObject,
        *,
        identity_height: float = 0,
    ) -> None:
        """등록된 채널을 개별 행으로 만들고 알림 종류를 한 열로 쌓는다."""
        self._actions = actions
        self._page, self._viewport = page, viewport
        # 전체 사용과 그룹 제목·행·간격을 같은 계산식으로 배치한다.
        required = 8 + 40 + GROUP_GAP + HEADING_HEIGHT + len(channels) * ROW_HEIGHT + GROUP_GAP
        self._base_height = (
            float(required) + identity_height + HEADING_HEIGHT + ROW_HEIGHT + GROUP_GAP
        )
        self.document = NSView.alloc().initWithFrame_(frame(0, 0, 584, viewport))
        page.setDocumentView_(self.document)
        self.fixed = NSView.alloc().initWithFrame_(frame(0, 0, 584, self._base_height))
        self.document.addSubview_(self.fixed)
        self.groups = NSView.alloc().initWithFrame_(frame(0, 0, 584, 0))
        self.document.addSubview_(self.groups)
        self._categories: tuple[SourceCategory, ...] | None = None
        self.inactive_rules: set[str] = set()
        self.source_types: dict[tuple[str, tuple[str, ...]], NSSwitch] = {}
        top = self._base_height - 8 - identity_height
        card(self.fixed, 16, top - 40, 552, 40)
        self.enabled = self._switch_row(self.fixed, "알림 사용", top, 40)
        top -= 40 + GROUP_GAP
        self._heading(self.fixed, "전달 채널", top)
        top -= HEADING_HEIGHT
        self.channels = self._rows(
            self.fixed, tuple((channel.key, channel.title) for channel in channels), top
        )
        top -= len(channels) * ROW_HEIGHT + GROUP_GAP
        self._heading(self.fixed, "퇴근 예정 알림 · 몇 분 전", top)
        top -= HEADING_HEIGHT
        card(self.fixed, 16, top - ROW_HEIGHT, 552, ROW_HEIGHT)
        self.departure = field(self.fixed, "", 28, top - 29, 412, "여러 시각은 쉼표로 구분")
        self.departure.setTarget_(actions)
        self.departure.setAction_("applyDeparture:")
        self.apply_departure = button(
            self.fixed, "적용", 458, top - 29, 96, actions, "applyDeparture:"
        )
        self._shown_offsets: tuple[int, ...] | None = None
        self.update_categories(())

    def read_offsets(self) -> tuple[int, ...]:
        """빈 입력은 모두 해제하고 중복되거나 유효하지 않은 시각은 저장하지 않는다."""
        raw = self.departure.stringValue().strip()
        offsets = []
        for item in raw.split(",") if raw else ():
            minutes = departure_minutes("departure_" + item.strip())
            if minutes is None:
                raise ValueError("퇴근 알림 시각을 0~1439분 사이의 정수로 입력하세요.")
            if minutes in offsets:
                raise ValueError("같은 퇴근 알림 시각이 두 번 입력되었습니다.")
            offsets.append(minutes)
        return tuple(sorted(offsets, reverse=True))

    def show_offsets(self, offsets: tuple[int, ...]) -> None:
        """다른 스위치가 갱신될 때 적용 전 입력을 덮어쓰지 않는다."""
        if offsets != self._shown_offsets:
            self._shown_offsets = offsets
            self.departure.setStringValue_(", ".join(map(str, offsets)))

    def update_categories(self, categories: tuple[SourceCategory, ...]) -> None:
        """저장된 카테고리 규칙을 표시하고 기존 폼과 스크롤 위치를 유지한다."""
        if categories == self._categories:
            return
        old_height = self.document.frame()[1][1]
        origin = self._page.contentView().bounds()[0][1]
        offset = max(0.0, old_height - self._viewport - origin)
        self._categories = categories
        self.groups.setSubviews_([])
        self.source_types.clear()
        self.inactive_rules = {
            entry.rule.notification_kind
            for category in categories
            if category.configuration is not None
            for entry in category.configuration.alerts
            if not entry.rule.enabled
        }
        ordered: list[tuple[str, str, dict[str, tuple[str, tuple[str, ...]]]]] = []
        for category in categories:
            choices = category_alert_choices(category)
            if choices:
                ordered.append((category.key, category.name, choices))
        extra = 58 + sum(
            HEADING_HEIGHT + len(rows) * ROW_HEIGHT + GROUP_GAP for _, _, rows in ordered
        )
        height = max(self._viewport, self._base_height + extra)
        self.document.setFrameSize_((584, height))
        self.fixed.setFrame_(frame(0, height - self._base_height, 584, self._base_height))
        self.groups.setFrame_(frame(0, height - self._base_height - extra, 584, extra))
        label(self.groups, "카테고리별 알림", 28, extra - 25, 312, bold=True)
        label(
            self.groups,
            "규칙을 추가하거나 채널을 바꾸려면 ‘카테고리 열기’를 누르세요.",
            28,
            extra - 46,
            410,
            size=11,
            secondary=True,
        )
        button(self.groups, "카테고리 열기", 430, extra - 32, 126, self._actions, "ruleSources:")
        top = extra - 58
        for key, title, rows in ordered:
            self._heading(self.groups, title, top)
            top -= HEADING_HEIGHT
            controls = self._rows(
                self.groups, tuple((choice, row[0]) for choice, row in rows.items()), top
            )
            for key_id in controls.keys() & self.inactive_rules:
                controls[key_id].setToolTip_("카테고리 설정에서 이 규칙 사용을 켜세요.")
            self.source_types.update(
                {(key, kinds): controls[choice] for choice, (_, kinds) in rows.items()}
            )
            top -= len(rows) * ROW_HEIGHT + GROUP_GAP
        self.document.scrollPoint_((0, max(0, height - self._viewport - offset)))

    def _heading(self, parent: NSView, title: str, top: float) -> None:
        """그룹 제목과 짧은 안내를 동일한 기준선에 맞춘다."""
        label(parent, title, 28, top - 18, 520, size=12, bold=True, secondary=True)

    def _rows(
        self, parent: NSView, items: tuple[tuple[str, str], ...], top: float
    ) -> dict[str, NSSwitch]:
        """각 채널·종류를 이름과 오른쪽 스위치의 독립적인 행으로 표시한다."""
        if not items:
            return {}
        card(parent, 16, top - len(items) * ROW_HEIGHT, 552, len(items) * ROW_HEIGHT)
        controls: dict[str, NSSwitch] = {}
        for index, (key, title) in enumerate(items):
            row_top = top - index * ROW_HEIGHT
            controls[key] = self._switch_row(parent, title, row_top, ROW_HEIGHT)
            if index:
                divider = NSBox.alloc().initWithFrame_(frame(28, row_top, 528, 1))
                divider.setBoxType_(NSBoxSeparator)
                parent.addSubview_(divider)
        return controls

    def _switch_row(self, parent: NSView, title: str, top: float, height: float) -> NSSwitch:
        """이름과 기본 macOS 스위치를 같은 세로 중앙에 배치한다."""
        caption = label(parent, title, 28, top - height, 450, size=13)
        place_label(caption, 28, top - height, 450, height)
        control = NSSwitch.alloc().initWithFrame_(
            frame(512, top - height + (height - 26) / 2, 44, 26)
        )
        control.setTarget_(self._actions)
        control.setAction_("changed:")
        control.setAccessibilityLabel_(title)
        parent.addSubview_(control)
        return control
