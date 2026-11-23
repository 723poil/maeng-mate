"""팝오버의 시스템 글꼴·색상·아이콘과 기본 컨트롤을 만든다."""

from AppKit import (
    NSAppearance,
    NSAppearanceNameAqua,
    NSAppearanceNameDarkAqua,
    NSBox,
    NSBoxSeparator,
    NSButton,
    NSColor,
    NSFont,
    NSGlassEffectView,
    NSGlassEffectViewStyleRegular,
    NSImage,
    NSImageView,
    NSTextField,
    NSView,
)
from Foundation import NSObject


def frame(
    x: float, y: float, width: float, height: float
) -> tuple[tuple[float, float], tuple[float, float]]:
    """Cocoa 프레임은 화면 배율과 무관한 포인트로 정의한다."""
    return ((x, y), (width, height))


def place_label(view: NSTextField, x: float, y: float, width: float, height: float) -> None:
    """글꼴의 실제 권장 높이를 영역 중앙에 배치해 위쪽 쏠림을 막는다."""
    text_height = min(height, view.fittingSize()[1])
    view.setFrame_(frame(x, y + (height - text_height) / 2, width, text_height))


def label(
    parent: NSView,
    value: str,
    x: float,
    y: float,
    width: float,
    *,
    size: float = 12,
    bold: bool = False,
    secondary: bool = False,
) -> NSTextField:
    """기본 시스템 글꼴과 의미 색상을 사용한다."""
    view = NSTextField.labelWithString_(value)
    view.setFont_(NSFont.boldSystemFontOfSize_(size) if bold else NSFont.systemFontOfSize_(size))
    view.setTextColor_(NSColor.secondaryLabelColor() if secondary else NSColor.labelColor())
    place_label(view, x, y, width, size + 7)
    parent.addSubview_(view)
    return view


def button(
    parent: NSView,
    value: str,
    x: float,
    y: float,
    width: float,
    target: NSObject,
    action: str,
    *,
    bordered: bool = True,
) -> NSButton:
    """실제 macOS 버튼을 콜백과 연결한다."""
    view = NSButton.buttonWithTitle_target_action_(value, target, action)
    view.setFrame_(frame(x, y, width, 26))
    view.setFont_(NSFont.systemFontOfSize_(12))
    view.setBordered_(bordered)
    parent.addSubview_(view)
    return view


def icon_button(
    parent: NSView, name: str, title: str, x: float, y: float, target: NSObject, action: str
) -> NSButton:
    """시스템 아이콘 버튼의 도움말과 VoiceOver 이름을 동작에 맞춘다."""
    control = button(parent, "", x, y, 30, target, action, bordered=False)
    control.setImage_(NSImage.imageWithSystemSymbolName_accessibilityDescription_(name, title))
    control.setToolTip_(title)
    control.setAccessibilityLabel_(title)
    return control


def symbol(parent: NSView, name: str, x: float, y: float) -> NSImageView:
    """외부 이미지 없이 SF Symbols로 섹션을 구별한다."""
    view = NSImageView.alloc().initWithFrame_(frame(x, y, 19, 19))
    view.setImage_(NSImage.imageWithSystemSymbolName_accessibilityDescription_(name, name))
    view.setContentTintColor_(NSColor.secondaryLabelColor())
    parent.addSubview_(view)
    return view


def separator(parent: NSView, y: float) -> None:
    """시스템 구분선을 얇게 배치한다."""
    view = NSBox.alloc().initWithFrame_(frame(20, y, 360, 1))
    view.setBoxType_(NSBoxSeparator)
    parent.addSubview_(view)


def glass_container(content: NSView, width: float, height: float) -> NSGlassEffectView:
    """macOS 26 기본 유리 효과 안에 화면 전체를 배치한다."""
    view = NSGlassEffectView.alloc().initWithFrame_(frame(0, 0, width, height))
    view.setStyle_(NSGlassEffectViewStyleRegular)
    corner_radius = 20.0
    view.setCornerRadius_(corner_radius)
    # 임의 형제 뷰가 아닌 정식 contentView를 써서 입력·글자 위의 효과 순서를 보장한다.
    view.setContentView_(content)
    # 유리 소재의 곡률과 실제 콘텐츠 클립을 맞춰 하단에 사각 배경이 남지 않게 한다.
    for surface in (view, content):
        surface.setWantsLayer_(True)
        layer = surface.layer()
        if layer is not None:
            layer.setBackgroundColor_(None)
            layer.setCornerRadius_(corner_radius)
            layer.setMasksToBounds_(True)
    return view


def card_background(appearance: NSAppearance) -> NSColor:
    """외관이 바뀔 때도 글자색에 맞는 카드 배경을 다시 결정한다."""
    mode = appearance.bestMatchFromAppearancesWithNames_(
        [NSAppearanceNameAqua, NSAppearanceNameDarkAqua]
    )
    return NSColor.colorWithCalibratedWhite_alpha_(
        0.12 if mode == NSAppearanceNameDarkAqua else 1.0, 0.90
    )


def card(
    parent: NSView,
    x: float,
    y: float,
    width: float,
    height: float,
    *,
    canvas: bool = False,
    input_area: bool = False,
) -> NSBox:
    """시스템 글자색을 읽기 쉬운 반투명 카드와 얇은 경계로 구분한다."""
    from AppKit import NSBoxCustom, NSNoBorder, NSNoTitle

    view = NSBox.alloc().initWithFrame_(frame(x, y, width, height))
    view.setBoxType_(NSBoxCustom)
    view.setTitlePosition_(NSNoTitle)
    view.setBorderType_(NSNoBorder)
    view.setBorderWidth_(0 if canvas else 0.7)
    view.setCornerRadius_(0 if canvas else 14 if not input_area else 7)
    color = (
        NSColor.windowBackgroundColor()
        if canvas
        else NSColor.textBackgroundColor()
        if input_area
        else NSColor.controlBackgroundColor()
    )
    view.setFillColor_(
        color.colorWithAlphaComponent_(0)
        if canvas
        else NSColor.colorWithName_dynamicProvider_(None, card_background)
    )
    view.setBorderColor_(NSColor.labelColor().colorWithAlphaComponent_(0.08))
    parent.addSubview_(view)
    return view
