"""Finder에서 여는 홈과 월간 캘린더를 같은 유리 창에 배치한다."""

import time
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum

from AppKit import (
    NSApplication,
    NSButton,
    NSLineBreakByTruncatingTail,
    NSNoBorder,
    NSScreen,
    NSScrollView,
    NSView,
    NSWorkspace,
)
from Foundation import NSObject, NSRunLoop, NSRunLoopCommonModes, NSTimer

from src.application.desktop import MenuEntry
from src.presentation.calendar_views import (
    COMPACT_CALENDAR_HEIGHT,
    CalendarPage,
    CompactCalendarView,
)
from src.presentation.entry_list import EntryList
from src.presentation.glass_panel import auxiliary_window
from src.presentation.native_views import button, card, frame, glass_container, icon_button, label
from src.presentation.preferences_sidebar import NavigationSidebar


@dataclass(frozen=True)
class HomeCard:
    """확정된 표시 순서로 전달하는 홈 카드와 현재 행이다."""

    key: str
    title: str
    rows: tuple[MenuEntry, ...] = ()
    message: str = ""


class MainPage(Enum):
    """메인 창에서 직접 이동할 수 있는 페이지다."""

    HOME = ("홈", "house")
    CALENDAR = ("캘린더", "calendar")

    def __init__(self, title: str, icon: str) -> None:
        """기존 설정 창과 같은 시스템 심볼 셀을 구성한다."""
        self.title, self.icon = title, icon


class WindowActions(NSObject):
    """창 탐색과 현재 목차 항목의 스크롤을 연결한다."""

    owner: "MainWindow"

    def jump_(self, sender: NSButton) -> None:
        """선택한 카드의 제목을 스크롤 영역 안에 표시한다."""
        header = self.owner.headers[sender.tag()]
        header.scrollRectToVisible_(header.bounds())

    def fold_(self, sender: object) -> None:
        """본문 너비를 바꾸고 같은 화면을 다시 배치한다."""
        self.owner.fold()

    def slide_(self, sender: object) -> None:
        """메인 루프에서 사이드바와 본문 위치를 함께 갱신한다."""
        self.owner.animate_sidebar()


class MainWindow:
    """기존 컨트롤과 표시 설정을 재사용하는 앱 기본 창이다."""

    def __init__(
        self,
        target: NSObject,
        calendar: Callable[[NSView], CalendarPage] | None,
        compact: Callable[[NSView, float], CompactCalendarView] | None,
    ) -> None:
        """사용 가능한 모니터 안에 창을 준비하고 Finder 요청 때만 표시한다."""
        screen = NSScreen.mainScreen()
        sw, sh = screen.visibleFrame()[1] if screen else (1100, 740)
        self.width, self.height = min(1080.0, sw - 16), min(700.0, sh - 16)
        self.window = auxiliary_window("Catch", self.width, self.height)
        self.target, self.calendar_factory, self.compact_factory = target, calendar, compact
        self.actions = WindowActions.alloc().init()
        self.actions.owner = self
        self.folded = self.calendar = False
        self.cards: tuple[HomeCard, ...] = ()
        self.empty = "설정 불러오는 중…"
        self.headers: list[NSView] = []
        self.page: CalendarPage | None = None
        self.compact: CompactCalendarView | None = None
        self.lists: dict[str, EntryList] = {}
        self._slide_timer: NSTimer | None = None
        self._side_width = 146.0
        self._layout()

    def _layout(self) -> None:
        """창 크기와 사이드바 상태에 맞게 탐색과 본문을 배치한다."""
        if self.page:
            self.page.filter_popover.hide()
        if self.compact:
            self.compact.filter_popover.hide()
        self.page = None
        h, w = self.height, self.width
        side = 0 if self.folded else 146
        root = NSView.alloc().initWithFrame_(frame(0, 0, w, h))
        self.window.setContentView_(glass_container(root, w, h))
        self.rail = NSView.alloc().initWithFrame_(frame(side - 146, 0, 146, h))
        root.addSubview_(self.rail)
        self.sidebar = NSView.alloc().initWithFrame_(frame(4, 44, 142, h - 160))
        self.rail.addSubview_(self.sidebar)
        pages = (MainPage.HOME, MainPage.CALENDAR) if self.calendar_factory else (MainPage.HOME,)
        self.navigation = NavigationSidebar(self.rail, h - 120, 84, pages, self._navigate)
        icon_button(self.rail, "gearshape", "설정 열기", 12, 8, self.target, "settings:")
        toggle_x = max(78, side + 8)
        self.toggle_button = icon_button(
            root,
            "sidebar.left",
            "사이드바 펼치기" if self.folded else "사이드바 접기",
            toggle_x,
            h - 36,
            self.actions,
            "fold:",
        )
        self.title = label(root, "", toggle_x + 36, h - 32, 150, size=16, bold=True)
        self.display_settings = icon_button(
            root,
            "slider.horizontal.3",
            "홈 표시 설정",
            w - 38,
            h - 36,
            self.target,
            "screenSettings:",
        )
        self.body = NSView.alloc().initWithFrame_(frame(side + 8, 8, w - side - 16, h - 50))
        root.addSubview_(self.body)
        self.scroll = self._scroll(self.body)
        self.nav_scroll = self._scroll(self.sidebar)
        self.calendar_host = NSView.alloc().initWithFrame_(self.body.bounds())
        self.body.addSubview_(self.calendar_host)
        self._home()
        self._select()

    def _scroll(self, parent: NSView) -> NSScrollView:
        """본문과 목차가 각각 넘칠 때 스크롤하도록 구성한다."""
        scroll = NSScrollView.alloc().initWithFrame_(parent.bounds())
        scroll.setHasVerticalScroller_(True)
        scroll.setAutohidesScrollers_(True)
        scroll.setDrawsBackground_(False)
        scroll.setBorderType_(NSNoBorder)
        parent.addSubview_(scroll)
        return scroll

    def fold(self) -> None:
        """사이드바를 접거나 펼쳐 달력과 홈의 실제 너비를 바꾼다."""
        self._stop_slide()
        self.folded = not self.folded
        self._slide_from = self._side_width
        self._slide_to = 0.0 if self.folded else 146.0
        # 마지막 폭에 맞춰 목록을 한 번만 배치해 매 프레임 달력 셀을 만들지 않는다.
        origin = self.scroll.contentView().bounds()[0][1]
        self._layout()
        document = self.scroll.documentView()
        if document is not None:
            document.scrollPoint_((0, origin))
        if (
            not self.window.isVisible()
            or NSWorkspace.sharedWorkspace().accessibilityDisplayShouldReduceMotion()
        ):
            self._place_sidebar(self._slide_to)
            return
        self._place_sidebar(self._slide_from)
        self._slide_start = time.monotonic()
        self._slide_timer = (
            NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
                1 / 60, self.actions, "slide:", None, True
            )
        )
        NSRunLoop.currentRunLoop().addTimer_forMode_(self._slide_timer, NSRunLoopCommonModes)

    def _place_sidebar(self, width: float) -> None:
        """고정된 오른쪽 가장자리를 기준으로 본문 폭과 도구 위치를 맞춘다."""
        self._side_width = width
        h = self.height
        self.rail.setFrame_(frame(width - 146, 0, 146, h))
        self.rail.setHidden_(width == 0)
        self.body.setFrame_(frame(width + 8, 8, self.width - width - 16, h - 50))
        self.body.setBoundsSize_((self.width - self._slide_to - 16, h - 50))
        x = max(78, width + 8)
        self.toggle_button.setFrame_(frame(x, h - 36, 30, 26))
        (_, y), size = self.title.frame()
        self.title.setFrame_(frame(x + 36, y, *size))

    def animate_sidebar(self) -> None:
        """빠른 재클릭은 직전 위치에서 반전하며 창이 닫히면 즉시 끝낸다."""
        progress = (
            min(1.0, (time.monotonic() - self._slide_start) / 0.2)
            if self.window.isVisible()
            else 1.0
        )
        self._place_sidebar(
            self._slide_to + (self._slide_from - self._slide_to) * (1 - progress) ** 3
        )
        if progress == 1:
            self._stop_slide()

    def _stop_slide(self) -> None:
        """지난 전환의 타이머가 새 창 상태를 덮어쓰지 않도록 정리한다."""
        if self._slide_timer is not None:
            self._slide_timer.invalidate()
            self._slide_timer = None

    def update(self, cards: tuple[HomeCard, ...], empty: str) -> None:
        """행 내용만 달라지면 기존 목록과 스크롤 위치를 유지한다."""
        if self.cards == cards and self.empty == empty:
            return

        def layout(values: tuple[HomeCard, ...]) -> tuple[tuple[str, str, str, int], ...]:
            """높이나 제목을 바꾸는 값만 비교한다."""
            return tuple((v.key, v.title, v.message, min(6, max(1, len(v.rows)))) for v in values)

        rebuild = layout(cards) != layout(self.cards) or empty != self.empty
        self.cards, self.empty = cards, empty
        if rebuild:
            self._home()
        else:
            for item in cards:
                if item.key in self.lists:
                    self.lists[item.key].update(item.rows)

    def _home(self) -> None:
        """보이는 카드만 목차와 같은 순서로 배치한다."""
        if self.compact:
            self.compact.filter_popover.hide()
        self.compact = None
        self.headers.clear()
        self.lists.clear()
        w, h = self.scroll.contentView().bounds()[1]
        heights = [
            COMPACT_CALENDAR_HEIGHT
            if item.key == "calendar"
            else 66
            if item.key == "work" or item.message
            else 38 + 29 * min(6, max(1, len(item.rows)))
            for item in self.cards
        ]
        top = max(h, sum(heights) + 8 * len(heights))
        doc = NSView.alloc().initWithFrame_(frame(0, 0, w, top))
        self.scroll.setDocumentView_(doc)
        # 항상 표시되는 스크롤 막대가 차지한 폭도 실제 클립 뷰에서 제외한다.
        w = self.scroll.contentView().bounds()[1][0]
        doc.setFrameSize_((w, top))
        nav_height = max(self.sidebar.frame()[1][1], 24 + 28 * len(self.cards))
        nav = NSView.alloc().initWithFrame_(frame(0, 0, 136, nav_height))
        self.nav_scroll.setDocumentView_(nav)
        if len(self.cards) > 1:
            label(nav, "바로가기", 14, nav_height - 20, 108, size=11, secondary=True)
        for index, (item, height) in enumerate(zip(self.cards, heights, strict=True)):
            y = top - height
            if item.key == "calendar" and self.compact_factory:
                self.compact = self.compact_factory(doc, w)
                self.compact.resize(y)
                header = self.compact.view
            else:
                card(doc, 0, y, w, height, glass=True)
                header = label(doc, item.title, 12, top - 26, w - 132, size=13, bold=True)
                if item.key == "work":
                    button(doc, "기록 ↗", w - 90, top - 32, 80, self.target, "records:")
                if item.message:
                    label(doc, item.message, 12, y + 8, w - 24, size=12, secondary=True)
                elif item.key != "work":
                    entries = EntryList(doc, 10, y + 4, w - 20, height - 36, self.target)
                    entries.update(item.rows)
                    self.lists[item.key] = entries
            self.headers.append(header)
            if len(self.cards) > 1:
                jump = button(
                    nav,
                    item.title,
                    14,
                    nav_height - 24 - 28 * (index + 1),
                    118,
                    self.actions,
                    "jump:",
                    bordered=False,
                )
                jump.setTag_(index)
                jump.setAlignment_(0)
                jump.setToolTip_(item.title)
                jump.cell().setLineBreakMode_(NSLineBreakByTruncatingTail)
            top = y - 8
        if not self.cards:
            label(doc, self.empty, 12, h - 34, w - 24, secondary=True)
        doc.scrollPoint_((0, doc.frame()[1][1] - h))
        nav.scrollPoint_((0, nav_height - self.sidebar.frame()[1][1]))

    def _navigate(self, index: int) -> None:
        """사이드바 선택으로 본문만 전환해 키보드 초점을 유지한다."""
        self.calendar = index == 1
        self._select()

    def _select(self) -> None:
        """선택한 본문과 목차만 보이고 숨은 달력 팝오버를 닫는다."""
        if not self.folded:
            self.navigation.select(MainPage.CALENDAR if self.calendar else MainPage.HOME)
        self.display_settings.setHidden_(self.calendar)
        self.title.setStringValue_("캘린더" if self.calendar else "홈")
        self.calendar_host.setHidden_(not self.calendar)
        self.scroll.setHidden_(self.calendar)
        self.nav_scroll.setHidden_(self.calendar)
        if self.calendar and self.calendar_factory and not self.page:
            self.page = self.calendar_factory(self.calendar_host)
        if self.page and not self.calendar:
            self.page.filter_popover.hide()
        if self.compact and self.calendar:
            self.compact.filter_popover.hide()

    def show(self, calendar: bool) -> None:
        """이미 열린 창을 재사용하고 현재 페이지를 앞으로 가져온다."""
        self.calendar = calendar and self.calendar_factory is not None
        self._select()
        NSApplication.sharedApplication().activateIgnoringOtherApps_(True)
        self.window.makeKeyAndOrderFront_(None)
