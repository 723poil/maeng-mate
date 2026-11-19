"""카테고리 카드의 업무 행과 원본 링크를 표시하는 네이티브 목록이다."""

from AppKit import (
    NSBox,
    NSColor,
    NSFont,
    NSLineBreakByTruncatingTail,
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


def status_badge(
    parent: NSView, status: str, y: float, *, x: float = 2, max_width: float = 128
) -> float:
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
    width = min(max_width, max(36.0, caption.fittingSize()[0] + 16))
    background = NSBox.alloc().initWithFrame_(frame(x, y, width, 18))
    background.setBoxType_(NSBoxCustom)
    background.setTitlePosition_(NSNoTitle)
    background.setBorderType_(NSNoBorder)
    background.setBorderWidth_(0)
    background.setCornerRadius_(5)
    background.setFillColor_(color.colorWithAlphaComponent_(0.14))
    parent.addSubview_(background)
    caption.setAlignment_(NSTextAlignmentCenter)
    caption.cell().setLineBreakMode_(NSLineBreakByTruncatingTail)
    place_label(caption, x, y, width, 18)
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
        self._rows: list[NSView] = []
        parent.addSubview_(self._scroll)

    def update(self, values: tuple[MenuEntry, ...]) -> None:
        """같은 자료의 새로고침은 사용자의 스크롤 위치를 바꾸지 않는다."""
        previous = self._values
        if previous == values:
            return
        height = max(self._height, len(values) * 29)
        document = self._scroll.documentView()
        if document is None or not previous or not values:
            # 첫 표시·빈 상태 전환·가로 폭 변경은 문서 전체를 배치한다.
            document = NSView.alloc().initWithFrame_(frame(0, 0, self._width, height))
            self._rows.clear()
            if not values:
                empty = label(
                    document, "없음", 2, height - 24, self._width - 12, size=12, secondary=True
                )
                empty.setAutoresizingMask_(NSViewMinYMargin)
        elif document.frame()[1][1] != height:
            document.setFrameSize_((self._width, height))
        # 기존 행은 위쪽 여백을 유지하므로 문서가 늘거나 줄어도 같은 순번에 남는다.
        for row in self._rows[len(values) :]:
            row.removeFromSuperview()
        del self._rows[len(values) :]
        for index, item in enumerate(values):
            if index < len(self._rows) and previous is not None and item == previous[index]:
                continue
            # 창에 붙이기 전에 글꼴을 측정해 첫 표시와 부분 갱신에서 같은 폭을 사용한다.
            row = NSView.alloc().initWithFrame_(
                frame(0, height - (index + 1) * 29, self._width, 29)
            )
            row.setAutoresizingMask_(NSViewMinYMargin)
            self._render_row(row, item)
            if index == len(self._rows):
                document.addSubview_(row)
                self._rows.append(row)
            else:
                document.replaceSubview_with_(self._rows[index], row)
                self._rows[index] = row
        if document is not self._scroll.documentView():
            self._scroll.setDocumentView_(document)
        self._values = values
        document.scrollPoint_((0, max(0, height - self._height)))

    def _render_row(self, view: NSView, item: MenuEntry) -> None:
        """변경된 행의 현재 표시 방식과 원본 링크로 배지·기한·버튼을 배치한다."""
        title_x = 0.0
        fields = item.display_fields
        if fields is not None:
            title_x, title = self._custom_title(view, item, fields)
        else:
            if item.status is not None:
                badge_width = status_badge(view, item.status, 5.5)
                label(view, "-", badge_width + 7, 5.5, 9, size=11, secondary=True)
                title_x = badge_width + 21
            # 상태를 사용하지 않는 소스도 독립적으로 연결한 기한·차시를 표시한다.
            middle = item.deadline or item.round_name
            if middle is not None:
                middle_label = label(
                    view,
                    middle,
                    title_x,
                    5.5,
                    120,
                    size=11,
                    secondary=True,
                )
                date_width = min(120.0, middle_label.fittingSize()[0] + 2)
                place_label(middle_label, title_x, 5.5, date_width, 18)
                label(
                    view,
                    "-",
                    title_x + date_width + 4,
                    5.5,
                    9,
                    size=11,
                    secondary=True,
                )
                title_x += date_width + 18
            title = " ".join(
                (item.title if item.status is not None else f"{item.detail}  {item.title}").split()
            )
        row = button(
            view,
            title,
            title_x,
            2,
            self._width - title_x - 12,
            self._target,
            "open:",
            bordered=False,
        )
        row.setFrame_(frame(title_x, 2, self._width - title_x - 12, 25))
        row.setAlignment_(0)
        row.cell().setRepresentedObject_(item.url)
        row.setEnabled_(item.url is not None)
        row.setToolTip_(
            "\n".join((item.title, *(f"{name}  {value or '—'}" for name, value in fields)))
            if fields
            else item.title
        )

    def _custom_title(
        self, view: NSView, item: MenuEntry, fields: tuple[tuple[str, str], ...]
    ) -> tuple[float, str]:
        """상태 배지를 앞에 고정하고 나머지 칼럼은 지정한 순서대로 제목 옆에 둔다."""
        x = 0.0
        inline = []
        for index, (name, value) in enumerate(fields):
            # 긴 배지나 여러 상태가 있어도 제목과 원본 열기 버튼의 공간을 남긴다.
            available = self._width - 110 - x
            if index in item.badge_field_indices and value and available >= 36:
                x += status_badge(view, value, 5.5, x=x + 2, max_width=min(128, available)) + 6
            else:
                # 빈칸과 폭을 넘는 상태도 제목·도움말에서 확인할 수 있다.
                inline.append((name, value))
        return x, display_line(item.title, tuple(inline))

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
