"""알림 설정을 설정 간격 체계의 카드로 구성한다."""

from collections.abc import Callable

from AppKit import NSPopUpButton, NSScrollView, NSSwitch, NSView
from Foundation import NSObject

from src.application.notifications import NotificationChannel
from src.domain.category_event_alerts import EVENT_ALERT_TRIGGERS
from src.domain.source_display import SourceCategory
from src.presentation.native_views import (
    CARD_GAP,
    CARD_PADDING,
    CARD_TITLE,
    CARD_WIDTH,
    FORM_CONTROL_X,
    FORM_LABEL_WIDTH,
    FORM_ROW,
    card,
    card_height,
    form_control_y,
    form_dividers,
    form_row_y,
    frame,
    label,
    place_label,
)

# 페이지 맨 위 카드와 스크롤 영역 위쪽 사이 여백이다.
PAGE_TOP = 8.0
# 퇴근 알림으로 고를 수 있는 기본 시각(분 전)이다. 예전에 저장한 다른 값은 항목으로 덧붙인다.
DEPARTURE_PRESETS = (0, 5, 10, 15, 30, 60)
DEPARTURE_WIDTH = 160.0
# 기본 스위치의 크기다.
SWITCH_WIDTH = 40.0
SWITCH_HEIGHT = 22.0


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


def departure_choice_title(offsets: tuple[int, ...]) -> str:
    """선택 메뉴 한 항목의 이름이다. 여러 시각이면 늦은 것부터 이어 쓴다."""
    if not offsets:
        return "받지 않음"
    return ", ".join(departure_title(minutes) for minutes in sorted(offsets, reverse=True))


def departure_title(minutes: int) -> str:
    """퇴근 알림 칩의 짧은 이름이다."""
    if minutes == 0:
        return "정시"
    if minutes % 60 == 0:
        return f"{minutes // 60}시간 전"
    return f"{minutes}분 전"


class DepartureActions(NSObject):
    """퇴근 알림 시각을 고르면 바로 저장을 요청한다."""

    owner: "NotificationPreferences"

    def departure_(self, sender: object) -> None:
        """선택 메뉴 변경을 설정 화면에 전달한다."""
        self.owner.departure_changed()


class NotificationPreferences:
    """알림 사용·권한, 전달 채널, 퇴근 알림, 카테고리 규칙을 카드로 쌓는다."""

    def __init__(
        self,
        page: NSScrollView,
        viewport: float,
        channels: tuple[NotificationChannel, ...],
        actions: NSObject,
        departure_changed: Callable[[], None],
        *,
        permission: bool = False,
    ) -> None:
        """고정 카드는 한 번 만들고 카테고리 카드만 바뀔 때 다시 그린다."""
        self._actions = actions
        self._departure_changed = departure_changed
        self._departure_actions = DepartureActions.alloc().init()
        self._departure_actions.owner = self
        self._page, self._viewport = page, viewport
        self.document = NSView.alloc().initWithFrame_(frame(0, 0, 584, viewport))
        page.setDocumentView_(self.document)

        general_rows = 2 if permission else 1
        self._general = self._card("알림", general_rows)
        self.enabled = self._switch_row(self._general, "알림 사용", 0, general_rows)
        # 권한 확인 줄은 알림 설정 화면이 제어기와 함께 채운다.
        self.permission_row: tuple[NSView, float] | None = (
            (self._general, form_row_y(card_height(general_rows), 1)) if permission else None
        )

        self._channels_card = self._card("전달 채널", max(1, len(channels)))
        self.channels = {
            channel.key: self._switch_row(self._channels_card, channel.title, index, len(channels))
            for index, channel in enumerate(channels)
        }

        self._departure = self._card("퇴근 알림", 1)
        departure_label = label(self._departure, "알림 시각", 0, 0, FORM_LABEL_WIDTH - 8)
        place_label(
            departure_label,
            CARD_PADDING,
            form_row_y(card_height(1), 0),
            FORM_LABEL_WIDTH - 8,
            FORM_ROW,
        )
        self.departure = NSPopUpButton.alloc().initWithFrame_pullsDown_(
            frame(
                FORM_CONTROL_X - 3,
                form_control_y(form_row_y(card_height(1), 0), 26),
                DEPARTURE_WIDTH,
                26,
            ),
            False,
        )
        self.departure.setTarget_(self._departure_actions)
        self.departure.setAction_("departure:")
        self.departure.setAccessibilityLabel_("퇴근 알림 시각")
        self._departure.addSubview_(self.departure)
        self._choices: list[tuple[int, ...]] = []
        self._shown_offsets: tuple[int, ...] | None = None
        self.show_offsets(())

        self.groups = NSView.alloc().initWithFrame_(frame(0, 0, 584, 0))
        self.document.addSubview_(self.groups)
        self._categories: tuple[SourceCategory, ...] | None = None
        self.inactive_rules: set[str] = set()
        self.source_types: dict[tuple[str, tuple[str, ...]], NSSwitch] = {}
        self.update_categories(())

    @property
    def departure_controls(self) -> tuple[NSPopUpButton, ...]:
        """알림 사용 여부에 따라 함께 켜고 끄는 퇴근 알림 입력이다."""
        return (self.departure,)

    def _card(self, title: str, rows: int) -> NSView:
        """제목과 rows줄짜리 카드를 담은 뷰를 만든다. 위치는 배치 때 정한다."""
        height = card_height(rows)
        view = NSView.alloc().initWithFrame_(frame(CARD_PADDING, 0, CARD_WIDTH, height))
        self.document.addSubview_(view)
        card(view, 0, 0, CARD_WIDTH, height)
        heading = label(view, title, 0, 0, 300, size=13, bold=True)
        place_label(heading, CARD_PADDING, height - CARD_PADDING - CARD_TITLE, 300, CARD_TITLE)
        form_dividers(view, height, rows)
        return view

    def _switch_row(self, parent: NSView, title: str, row: int, rows: int) -> NSSwitch:
        """이름은 왼쪽, 기본 스위치는 카드 오른쪽 여백에 맞춘다."""
        y = form_row_y(card_height(rows), row)
        caption = label(parent, title, 0, 0, 300)
        place_label(caption, CARD_PADDING, y, 300, FORM_ROW)
        control = NSSwitch.alloc().initWithFrame_(
            frame(
                CARD_WIDTH - CARD_PADDING - SWITCH_WIDTH,
                y + (FORM_ROW - SWITCH_HEIGHT) / 2,
                SWITCH_WIDTH,
                SWITCH_HEIGHT,
            )
        )
        control.setTarget_(self._actions)
        control.setAction_("changed:")
        control.setAccessibilityLabel_(title)
        parent.addSubview_(control)
        return control

    def departure_changed(self) -> None:
        """선택 메뉴에서 고른 값을 설정 화면에 저장하게 한다."""
        self._departure_changed()

    def read_offsets(self) -> tuple[int, ...]:
        """선택한 퇴근 알림 시각을 늦은 것부터 반환한다."""
        index = self.departure.indexOfSelectedItem()
        offsets = self._choices[index] if 0 <= index < len(self._choices) else ()
        return tuple(sorted(offsets, reverse=True))

    def show_offsets(self, offsets: tuple[int, ...]) -> None:
        """저장된 값을 선택하고, 기본 항목에 없는 값(여러 시각 포함)은 항목을 덧붙여 보존한다."""
        if offsets == self._shown_offsets:
            return
        self._shown_offsets = offsets
        current = tuple(sorted(offsets, reverse=True))
        self._choices = [(), *((minutes,) for minutes in DEPARTURE_PRESETS)]
        if current not in self._choices:
            self._choices.append(current)
        self.departure.removeAllItems()
        self.departure.addItemsWithTitles_(
            [departure_choice_title(choice) for choice in self._choices]
        )
        self.departure.selectItemAtIndex_(self._choices.index(current))

    def update_categories(self, categories: tuple[SourceCategory, ...]) -> None:
        """카테고리 규칙 카드를 다시 그리고 기존 폼과 스크롤 위치를 유지한다."""
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
        ordered = [
            (category.key, category.name, choices)
            for category in categories
            if (choices := category_alert_choices(category))
        ]
        fixed = (self._general, self._channels_card, self._departure)
        fixed_height = PAGE_TOP + sum(view.frame()[1][1] + CARD_GAP for view in fixed)
        group_heights = [card_height(len(rows)) for _, _, rows in ordered]
        # 규칙이 하나도 없을 때만 규칙을 만드는 곳을 한 줄로 알려 준다.
        groups_height = (
            sum(height + CARD_GAP for height in group_heights) if ordered else FORM_ROW + CARD_GAP
        )
        height = max(self._viewport, fixed_height + groups_height)
        self.document.setFrameSize_((584, height))
        top = height - PAGE_TOP
        for view in fixed:
            view_height = view.frame()[1][1]
            view.setFrame_(frame(CARD_PADDING, top - view_height, CARD_WIDTH, view_height))
            top -= view_height + CARD_GAP
        self.groups.setFrame_(frame(0, top - groups_height, 584, groups_height))
        top = groups_height
        if not ordered:
            hint = label(
                self.groups,
                "알림 규칙은 카테고리 설정에서 만들 수 있습니다.",
                0,
                0,
                CARD_WIDTH - CARD_PADDING,
                size=12,
                secondary=True,
            )
            place_label(hint, CARD_PADDING * 2, top - FORM_ROW, CARD_WIDTH - CARD_PADDING, FORM_ROW)
        for (key, title, rows), group_height in zip(ordered, group_heights, strict=True):
            view = NSView.alloc().initWithFrame_(
                frame(CARD_PADDING, top - group_height, CARD_WIDTH, group_height)
            )
            self.groups.addSubview_(view)
            card(view, 0, 0, CARD_WIDTH, group_height)
            form_dividers(view, group_height, len(rows))
            heading = label(view, title, 0, 0, 300, size=13, bold=True)
            place_label(
                heading, CARD_PADDING, group_height - CARD_PADDING - CARD_TITLE, 300, CARD_TITLE
            )
            for index, (choice, (name, kinds)) in enumerate(rows.items()):
                control = self._switch_row(view, name, index, len(rows))
                if choice in self.inactive_rules:
                    control.setToolTip_("카테고리 설정에서 이 규칙 사용을 켜세요.")
                self.source_types[(key, kinds)] = control
            top -= group_height + CARD_GAP
        self.document.scrollPoint_((0, max(0, height - self._viewport - offset)))
