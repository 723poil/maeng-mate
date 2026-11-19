"""왼쪽 소스 라벨에만 표시하는 읽기 전용 속성 안내 팝오버다."""

from dataclasses import dataclass

from AppKit import (
    NSBox,
    NSBoxSeparator,
    NSColor,
    NSFont,
    NSMaxXEdge,
    NSPopover,
    NSPopoverBehaviorTransient,
    NSTextField,
    NSTrackingActiveInKeyWindow,
    NSTrackingArea,
    NSTrackingInVisibleRect,
    NSTrackingMouseEnteredAndExited,
    NSView,
    NSViewController,
)
from Foundation import NSTimer

from src.presentation.native_views import card, frame, label, place_label, symbol


@dataclass(frozen=True)
class PropertyHelpRow:
    """속성명·연결값·용도를 따로 배치하기 위한 표시 값이다."""

    name: str
    column: str
    purpose: str


def property_help_rows(summary: str) -> tuple[PropertyHelpRow, ...]:
    """서버의 읽기용 설명을 분리하며 속성 이름 안의 구두점은 유지한다."""
    result: list[PropertyHelpRow] = []
    for text in summary.splitlines()[1:-1]:
        if "): " in text and " (" in text:
            left, purpose = text.rsplit("): ", 1)
            name, column = left.split(" (", 1)
        else:
            name, separator, purpose = text.partition(": ")
            column = ""
            if not separator:
                purpose = name
                name = "정보"
        result.append(PropertyHelpRow(name, column, purpose))
    return tuple(result)


class SourceHelpPopover:
    """기본 macOS 팝오버에 헤더·속성 행·짧은 안내를 구성한다."""

    def __init__(self, title: str, summary: str) -> None:
        """전달받은 설명만 그리며 네트워크·설정을 읽지 않는다."""
        rows = property_help_rows(summary)
        width, height = 460.0, 100.0 + len(rows) * 44
        self.content = NSView.alloc().initWithFrame_(frame(0, 0, width, height))
        card(self.content, 0, 0, width, height)
        symbol(self.content, "square.stack.3d.up", 16, height - 37)
        label(self.content, title + " 수집 정보", 43, height - 37, width - 59, size=14, bold=True)
        label(self.content, "속성", 16, height - 61, 132, size=10, secondary=True)
        label(self.content, "사용 용도", 164, height - 61, 280, size=10, secondary=True)
        for index, row in enumerate(rows):
            top = height - 64 - index * 44
            name = label(self.content, row.name, 16, top - 22, 132, size=12, bold=True)
            if row.column:
                label(self.content, row.column, 16, top - 39, 132, size=10, secondary=True)
            else:
                place_label(name, 16, top - 44, 132, 44)
            purpose = label(self.content, row.purpose, 164, top - 44, 280, size=12)
            place_label(purpose, 164, top - 44, 280, 44)
            if index < len(rows) - 1:
                line = NSBox.alloc().initWithFrame_(frame(16, top - 44, 428, 1))
                line.setBoxType_(NSBoxSeparator)
                self.content.addSubview_(line)
        label(
            self.content,
            "링크를 바꾸면 열리는 Notion 페이지만 달라집니다.",
            16,
            12,
            428,
            size=10,
            secondary=True,
        )
        controller = NSViewController.alloc().init()
        controller.setView_(self.content)
        self.popover = NSPopover.alloc().init()
        self.popover.setContentViewController_(controller)
        self.popover.setContentSize_((width, height))
        self.popover.setBehavior_(NSPopoverBehaviorTransient)
        self.popover.setAnimates_(False)

    def show(self, anchor: NSView) -> None:
        """라벨 옆에 표시하고 창의 입력 초점을 옮기지 않는다."""
        self.popover.showRelativeToRect_ofView_preferredEdge_(anchor.bounds(), anchor, NSMaxXEdge)

    def hide(self) -> None:
        """라벨을 벗어나거나 페이지가 닫히면 안내도 닫는다."""
        self.popover.performClose_(None)


class SourceHelpLabel(NSTextField):
    """URL 입력칸과 분리된 라벨 전용 호버 영역이다."""

    def configureTitle_summary_(self, title: str, summary: str) -> None:
        """일반 툴팁 대신 짧은 지연 뒤 구조화된 안내를 연다."""
        self._summary = summary
        self._help = SourceHelpPopover(title, summary)
        self._timer: NSTimer | None = None
        self._hovering = False
        self.setStringValue_(title + ("  ⓘ" if summary else ""))
        self.setEditable_(False)
        self.setSelectable_(False)
        self.setBezeled_(False)
        self.setBordered_(False)
        self.setDrawsBackground_(False)
        self.setFont_(NSFont.systemFontOfSize_(13))
        self.setTextColor_(NSColor.labelColor())
        self.setAccessibilityHelp_(summary)
        self._tracking = NSTrackingArea.alloc().initWithRect_options_owner_userInfo_(
            self.bounds(),
            NSTrackingMouseEnteredAndExited | NSTrackingActiveInKeyWindow | NSTrackingInVisibleRect,
            self,
            None,
        )
        self.addTrackingArea_(self._tracking)

    def mouseEntered_(self, event: object) -> None:
        """잠깐 지나가는 포인터에는 안내를 열지 않는다."""
        self._hovering = True
        if self._summary and self._timer is None:
            self._timer = NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
                0.25, self, "showInfo:", None, False
            )

    def showInfo_(self, sender: object) -> None:
        """닫힌 창에는 지연 안내를 다시 표시하지 않는다."""
        self._timer = None
        window = self.window()
        if self._hovering and window is not None and window.isVisible():
            self._help.show(self)

    def mouseExited_(self, event: object) -> None:
        """마우스가 라벨에서 벗어나면 입력칸까지 따라가지 않는다."""
        self.hideHelp()

    def hideHelp(self) -> None:
        """페이지 이동·닫기·행 갱신 때 예약과 팝오버를 함께 정리한다."""
        self._hovering = False
        if self._timer is not None:
            self._timer.invalidate()
            self._timer = None
        self._help.hide()
