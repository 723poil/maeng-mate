"""알림 설정을 채널별 스위치 행과 복수 시각 선택으로 구성한다."""

from AppKit import (
    NSBox,
    NSBoxSeparator,
    NSButton,
    NSPushOnPushOffButton,
    NSScrollView,
    NSSwitch,
    NSView,
)
from Foundation import NSObject

from src.application.notifications import NotificationChannel
from src.domain.category_event_alerts import EVENT_ALERT_KINDS, EVENT_ALERT_OPTIONS
from src.domain.notification_timing import DEPARTURE_REMINDER_MINUTES
from src.domain.preferences import Preferences
from src.domain.source_alerts import is_source_alert_kind
from src.domain.source_display import SourceDisplay, display_groups
from src.presentation.native_views import button, card, frame, label, place_label

ROW_HEIGHT = 34
GROUP_GAP = 12
HEADING_HEIGHT = 22


def category_alert_choices(
    sources: tuple[SourceDisplay, ...],
) -> dict[str, tuple[str, tuple[str, ...]]]:
    """두 화면에 같은 이름·순서·알림 키를 사용한다."""
    choices: dict[str, tuple[str, tuple[str, ...]]] = {}
    for source in sources:
        for kind, title in source.events:
            identity = kind if is_source_alert_kind(kind) else EVENT_ALERT_KINDS.get(kind, kind)
            previous = choices.get(identity, (title, ()))[1]
            choices[identity] = (title, tuple(dict.fromkeys((*previous, kind))))
    return choices


def event_alert_states(
    group: str, display: tuple[SourceDisplay, ...], preferences: Preferences | None
) -> dict[str, bool]:
    """카테고리 편집에 알림 화면과 같은 스위치 값을 제공한다."""
    value = preferences or Preferences()
    choices = category_alert_choices(tuple(item for item in display if item.key == group))
    return {
        item.trigger: any(
            value.notification_enabled(kind, group)
            for kind in choices.get(item.trigger, ("", item.kinds))[1]
        )
        for item in EVENT_ALERT_OPTIONS
    }


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
        self._display: tuple[SourceDisplay, ...] | None = None
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
        self.types: dict[str, NSButton | NSSwitch] = {}
        self._heading(self.fixed, "퇴근 예정 알림", top)
        top -= HEADING_HEIGHT
        card(self.fixed, 16, top - ROW_HEIGHT, 552, ROW_HEIGHT)
        for index, minutes in enumerate(DEPARTURE_REMINDER_MINUTES):
            control = button(
                self.fixed, f"{minutes}분 전", 28 + index * 108, top - 30, 96, actions, "changed:"
            )
            control.setButtonType_(NSPushOnPushOffButton)
            self.types[f"departure_{minutes}"] = control
        self.update_sources(())

    def update_sources(self, display: tuple[SourceDisplay, ...]) -> None:
        """활성 카테고리의 알림만 추가하고 기존 폼과 스크롤 위치를 유지한다."""
        if display == self._display:
            return
        old_height = self.document.frame()[1][1]
        origin = self._page.contentView().bounds()[0][1]
        offset = max(0.0, old_height - self._viewport - origin)
        self._display = display
        self.groups.setSubviews_([])
        self.source_types.clear()
        ordered: list[tuple[str, str, dict[str, tuple[str, tuple[str, ...]]]]] = []
        for key, sources in display_groups(display).items():
            choices = category_alert_choices(sources)
            if choices:
                ordered.append((key, sources[0].title, choices))
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
