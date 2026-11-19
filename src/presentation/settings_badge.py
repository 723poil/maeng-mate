"""설정 상태에 공통으로 사용하는 작은 색상 배지다."""

from AppKit import (
    NSBox,
    NSBoxCustom,
    NSColor,
    NSLineBreakByTruncatingTail,
    NSNoBorder,
    NSNoTitle,
    NSTextAlignmentCenter,
    NSView,
)

from src.presentation.native_views import frame, label, place_label


class SettingsBadge:
    """색상과 문구를 함께 표시하는 시스템 글꼴 배지다."""

    def __init__(self, parent: NSView, x: float, y: float, width: float) -> None:
        """고정 높이 안에서 글자 중심을 맞춘다."""
        self._x, self._y, self._width = x, y, width
        self._box = NSBox.alloc().initWithFrame_(frame(x, y, width, 20))
        self._box.setBoxType_(NSBoxCustom)
        self._box.setTitlePosition_(NSNoTitle)
        self._box.setBorderType_(NSNoBorder)
        self._box.setCornerRadius_(6)
        parent.addSubview_(self._box)
        self._text = label(parent, "", x, y, width, size=11, bold=True)
        self._text.setAlignment_(NSTextAlignmentCenter)
        self._text.cell().setLineBreakMode_(NSLineBreakByTruncatingTail)

    def update(self, text: str, tone: str) -> None:
        """공통 색조와 중앙 정렬을 유지하며 문구를 바꾼다."""
        color = {
            "green": NSColor.systemGreenColor(),
            "red": NSColor.systemRedColor(),
            "orange": NSColor.systemOrangeColor(),
            "blue": NSColor.systemBlueColor(),
            "purple": NSColor.systemPurpleColor(),
            "neutral": NSColor.secondaryLabelColor(),
        }[tone]
        self._box.setFillColor_(color.colorWithAlphaComponent_(0.14))
        self._text.setTextColor_(color)
        self._text.setStringValue_(text)
        place_label(self._text, self._x, self._y, self._width, 20)

    def set_tooltip(self, value: str) -> None:
        """상태의 자세한 결과는 배지 위에서만 표시한다."""
        self._box.setToolTip_(value)
        self._text.setToolTip_(value)
