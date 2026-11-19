"""카테고리 카드의 업무 행과 원본 링크를 표시하는 네이티브 목록이다."""

from AppKit import (
    NSBox,
    NSColor,
    NSFont,
    NSTextAlignmentCenter,
    NSTextField,
    NSView,
    NSViewMinYMargin,
)
from Foundation import NSObject

from src.application.desktop import MenuEntry
from src.domain.display_columns import display_line
from src.presentation.native_views import button, frame, label, place_label
from src.presentation.task_appearance import status_tone


def status_badge(parent: NSView, status: str, y: float) -> float:
    """색과 원문 상태를 함께 표시해 색상만으로 구별하지 않게 한다."""
    from AppKit import NSBoxCustom, NSNoBorder, NSNoTitle

    colors = {
        "purple": NSColor.systemPurpleColor(),
        "orange": NSColor.systemOrangeColor(),
        "blue": NSColor.systemBlueColor(),
        "green": NSColor.systemGreenColor(),
        "neutral": NSColor.secondaryLabelColor(),
    }
    color = colors[status_tone(status)]
    caption = NSTextField.labelWithString_(status)
    caption.setFont_(NSFont.boldSystemFontOfSize_(10))
    # 글꼴과 셀 안쪽 여백까지 측정해 영문 글자의 끝이 잘리지 않게 한다.
    width = min(128.0, max(36.0, caption.fittingSize()[0] + 16))
    background = NSBox.alloc().initWithFrame_(frame(2, y, width, 18))
    background.setBoxType_(NSBoxCustom)
    background.setTitlePosition_(NSNoTitle)
    background.setBorderType_(NSNoBorder)
    background.setBorderWidth_(0)
    background.setCornerRadius_(5)
    background.setFillColor_(color.colorWithAlphaComponent_(0.14))
    parent.addSubview_(background)
    caption.setAlignment_(NSTextAlignmentCenter)
    place_label(caption, 2, y, width, 18)
    caption.setTextColor_(color)
    caption.setToolTip_(status)
    parent.addSubview_(caption)
    return width


class EntryList:
    """항목이 늘어날 때만 스크롤하는 고정 높이의 네이티브 목록이다."""

    def __init__(
        self, parent: NSView, x: float, y: float, width: float, height: float, target: NSObject
    ) -> None:
        """스크롤 컨테이너와 콜백 대상은 한 번만 만든다."""
        from AppKit import NSNoBorder, NSScrollView

        self._scroll = NSScrollView.alloc().initWithFrame_(frame(x, y, width, height))
        self._scroll.setHasVerticalScroller_(True)
        self._scroll.setAutohidesScrollers_(True)
        self._scroll.setDrawsBackground_(False)
        self._scroll.setBorderType_(NSNoBorder)
        self._width = width
        self._height = height
        self._target = target
        self._values: tuple[MenuEntry, ...] | None = None
        parent.addSubview_(self._scroll)

    def update(self, values: tuple[MenuEntry, ...]) -> None:
        """같은 자료의 새로고침은 사용자의 스크롤 위치를 바꾸지 않는다."""
        if self._values == values:
            return
        self._values = values
        height = max(self._height, len(values) * 29)
        document = NSView.alloc().initWithFrame_(frame(0, 0, self._width, height))
        if not values:
            label(document, "없음", 2, height - 24, self._width - 12, size=12, secondary=True)
        cursor = height
        for item in values:
            cursor -= 29
            title_x = 0.0
            if item.display_fields is None:
                if item.status is not None:
                    badge_width = status_badge(document, item.status, cursor + 5.5)
                    label(document, "-", badge_width + 7, cursor + 5.5, 9, size=11, secondary=True)
                    title_x = badge_width + 21
                # 상태를 사용하지 않는 소스도 독립적으로 연결한 기한·차시를 표시한다.
                middle = item.deadline or item.round_name
                if middle is not None:
                    middle_label = label(
                        document,
                        middle,
                        title_x,
                        cursor + 5.5,
                        120,
                        size=11,
                        secondary=True,
                    )
                    date_width = min(120.0, middle_label.fittingSize()[0] + 2)
                    place_label(middle_label, title_x, cursor + 5.5, date_width, 18)
                    label(
                        document,
                        "-",
                        title_x + date_width + 4,
                        cursor + 5.5,
                        9,
                        size=11,
                        secondary=True,
                    )
                    title_x += date_width + 18
            fields = item.display_fields
            title = (
                display_line(item.title, fields)
                if fields is not None
                else " ".join(
                    (
                        item.title if item.status is not None else f"{item.detail}  {item.title}"
                    ).split()
                )
            )
            title_y = cursor + 2
            row = button(
                document,
                title,
                title_x,
                title_y,
                self._width - title_x - 12,
                self._target,
                "open:",
                bordered=False,
            )
            row.setFrame_(frame(title_x, title_y, self._width - title_x - 12, 25))
            row.setAlignment_(0)
            row.cell().setRepresentedObject_(item.url)
            row.setEnabled_(item.url is not None)
            row.setToolTip_(
                "\n".join((item.title, *(f"{name}  {value or '—'}" for name, value in fields)))
                if fields
                else item.title
            )
        # 문서 높이가 바뀌어도 배지·제목·빈 상태를 위쪽 여백에 고정한다.
        for child in document.subviews():
            child.setAutoresizingMask_(NSViewMinYMargin)
        self._scroll.setDocumentView_(document)
        document.scrollPoint_((0, max(0, height - self._height)))

    def resize(self, x: float, y: float, width: float, height: float) -> None:
        """확대·축소해도 위에서 읽던 항목의 위치를 유지한다."""
        document = self._scroll.documentView()
        top_distance = 0.0
        if document is not None:
            origin = self._scroll.contentView().bounds()[0][1]
            top_distance = max(0.0, document.frame()[1][1] - self._height - origin)
        values = self._values
        old_width = self._width
        self._width, self._height = width, height
        self._scroll.setFrame_(frame(x, y, width, height))
        if values is None:
            return
        needed_height = max(height, len(values) * 29)
        if document is None or old_width != width:
            # 가로 폭은 텍스트·버튼 측정에 영향을 주므로 이때만 행을 다시 구성한다.
            self._values = None
            self.update(values)
            document = self._scroll.documentView()
        elif document.frame()[1][1] != needed_height:
            document.setFrameSize_((width, needed_height))
        if document is not None:
            document.scrollPoint_((0, max(0, needed_height - height - top_distance)))
