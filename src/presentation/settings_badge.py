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

# 배지 글자 좌우 여백이다. 배지는 글자 폭에 맞춰 줄어든다.
BADGE_PADDING = 8.0


class SettingsBadge:
    """색상과 문구를 함께 표시하는 시스템 글꼴 배지다."""

    def __init__(
        self, parent: NSView, x: float, y: float, width: float, *, align: str = "right"
    ) -> None:
        """주어진 칸 안에서 글자 폭만큼만 차지하고 칸의 오른쪽(또는 왼쪽)에 붙는다."""
        self._x, self._y, self._width, self._align = x, y, width, align
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
        # 칸 폭을 넘지 않는 선에서 글자 폭 + 좌우 여백만큼만 배지를 그린다.
        width = min(self._width, self._text.fittingSize()[0] + 2 * BADGE_PADDING) if text else 0
        x = self._x + self._width - width if self._align == "right" else self._x
        self._box.setFrame_(frame(x, self._y, width, 20))
        self._box.setHidden_(not text)
        place_label(self._text, x, self._y, width, 20)

    def set_tooltip(self, value: str) -> None:
        """상태의 자세한 결과는 배지 위에서만 표시한다."""
        self._box.setToolTip_(value)
        self._text.setToolTip_(value)
