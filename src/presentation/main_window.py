"""Finder에서 여는 홈과 월간 캘린더를 같은 유리 창에 배치한다."""

from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum

from AppKit import NSApplication, NSButton, NSNoBorder, NSScreen, NSScrollView, NSView
from Foundation import NSObject

from src.application.desktop import MenuEntry
from src.presentation.calendar_views import (
    COMPACT_CALENDAR_HEIGHT,
    CalendarPage,
    CompactCalendarView,
)
from src.presentation.entry_list import EntryList
from src.presentation.glass_panel import auxiliary_window
from src.presentation.native_views import button, card, frame, glass_container, label
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
        self.window = auxiliary_window("맹메이트", self.width, self.height)
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
        self.sidebar = NSView.alloc().initWithFrame_(frame(4, 44, side, h - 160))
        root.addSubview_(self.sidebar)
        self.sidebar.setHidden_(self.folded)
        if not self.folded:
            pages = (
                (MainPage.HOME, MainPage.CALENDAR) if self.calendar_factory else (MainPage.HOME,)
            )
            self.navigation = NavigationSidebar(root, h - 120, 84, pages, self._navigate)
            button(root, "설정…", 8, 10, 126, self.target, "settings:")
        button(root, "사이드바", max(48, side + 4), h - 36, 80, self.actions, "fold:")
        self.title = label(root, "", max(134, side + 90), h - 30, 150, size=16, bold=True)
        button(root, "화면 설정…", w - 104, h - 36, 96, self.target, "screenSettings:")
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
        self.folded = not self.folded
        self._layout()

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
        w, h = self.body.frame()[1]
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
        nav_height = max(self.sidebar.frame()[1][1], 28 * len(self.cards))
        nav = NSView.alloc().initWithFrame_(frame(0, 0, 136, nav_height))
        self.nav_scroll.setDocumentView_(nav)
        for index, (item, height) in enumerate(zip(self.cards, heights, strict=True)):
            y = top - height
            if item.key == "calendar" and self.compact_factory:
                self.compact = self.compact_factory(doc, w)
                self.compact.resize(y)
                header = self.compact.view
            else:
                card(doc, 0, y, w, height)
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
                button(
                    nav,
                    item.title,
                    0,
                    nav_height - 28 * (index + 1),
                    132,
                    self.actions,
                    "jump:",
                    bordered=False,
                ).setTag_(index)
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
