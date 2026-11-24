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
    NSFontWeightMedium,
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


# 설정 화면 간격 체계(pt). 설정 카드는 모두 이 값으로 배치한다.
CARD_PADDING = 16.0  # 카드 안쪽 상하좌우 여백이며 페이지 좌우 여백과 같다.
CARD_GAP = 12.0  # 카드 사이 간격
CARD_WIDTH = 552.0  # 설정 페이지 폭 584에서 좌우 여백을 뺀 카드 폭
CARD_TITLE = 20.0  # 카드 제목 줄 높이
CARD_TITLE_GAP = 0.0  # 카드 제목과 첫 줄 사이. 줄 자체에 위아래 여백이 있어 따로 두지 않는다.
# 시스템 설정의 묶음 목록처럼 한 줄은 44pt이고 줄끼리 맞붙어 경계에 구분선만 긋는다.
FORM_ROW = 44.0  # 입력 한 줄 높이(위아래 여백 포함)
FORM_ROW_GAP = 0.0  # 입력 줄 사이 간격
CARD_BOTTOM = 4.0  # 마지막 줄 아래 여백. 줄 안쪽 여백과 합쳐 위쪽 여백과 비슷하게 보인다.
FORM_LABEL_WIDTH = 112.0  # 왼쪽 항목 이름 칸 폭
FORM_CONTROL_X = CARD_PADDING + FORM_LABEL_WIDTH  # 입력이 시작하는 x
# 테두리 있는 버튼은 프레임 안쪽에 6pt 여백을 두고 그려지므로, 오른쪽 끝을 맞출 때 그만큼 바깥으로 민다.
BUTTON_BEZEL_INSET = 6.0


# 켜고 끄는 선택 칩(요일·알림 시각)의 높이·모서리·글자 크기와 칩 사이 간격이다.
CHIP_HEIGHT = 22.0
CHIP_RADIUS = 6.0
CHIP_FONT = 12.0
CHIP_GAP = 6.0


class Chip:
    """둥근 사각형 바탕과 그 위 테두리 없는 버튼으로 된 선택 칩이다."""

    def __init__(
        self,
        parent: NSView,
        title: str,
        x: float,
        row_y: float,
        target: NSObject,
        action: str,
        *,
        width: float | None = None,
    ) -> None:
        """입력 줄 가운데에 놓고 폭을 주지 않으면 글자 폭에 맞춘다."""
        from AppKit import NSBoxCustom, NSNoBorder, NSNoTitle

        self.button = button(parent, title, x, row_y, 40, target, action, bordered=False)
        self.button.setFont_(NSFont.systemFontOfSize_weight_(CHIP_FONT, NSFontWeightMedium))
        self.width = width if width is not None else self.button.fittingSize()[0] + 14
        y = row_y + (FORM_ROW - CHIP_HEIGHT) / 2
        self.background = NSBox.alloc().initWithFrame_(frame(x, y, self.width, CHIP_HEIGHT))
        self.background.setBoxType_(NSBoxCustom)
        self.background.setTitlePosition_(NSNoTitle)
        self.background.setBorderType_(NSNoBorder)
        self.background.setCornerRadius_(CHIP_RADIUS)
        # 바탕을 버튼 아래에 두어 클릭은 버튼이 받는다.
        parent.addSubview_positioned_relativeTo_(self.background, -1, self.button)
        self.button.setFrame_(frame(x, y, self.width, CHIP_HEIGHT))
        self.set_selected(False)

    def set_selected(self, selected: bool) -> None:
        """켜지면 강조색 바탕과 흰 글자, 꺼지면 옅은 회색 바탕과 기본 글자다."""
        self.background.setFillColor_(
            NSColor.controlAccentColor()
            if selected
            else NSColor.secondaryLabelColor().colorWithAlphaComponent_(0.16)
        )
        self.button.setContentTintColor_(NSColor.whiteColor() if selected else NSColor.labelColor())


def card_height(rows: int) -> float:
    """제목 한 줄과 입력 rows줄을 담는 설정 카드의 높이다."""
    body = rows * FORM_ROW + max(0, rows - 1) * FORM_ROW_GAP
    return CARD_PADDING + CARD_TITLE + CARD_TITLE_GAP + body + CARD_BOTTOM


def form_divider(parent: NSView, y: float) -> NSBox:
    """카드 안쪽 여백 사이를 가로지르는 줄 구분선이다."""
    divider = NSBox.alloc().initWithFrame_(frame(CARD_PADDING, y, CARD_WIDTH - 2 * CARD_PADDING, 1))
    divider.setBoxType_(NSBoxSeparator)
    parent.addSubview_(divider)
    return divider


def form_dividers(parent: NSView, height: float, rows: int) -> None:
    """높이 height인 카드의 입력 줄 사이마다 줄 간격 가운데에 구분선을 긋는다."""
    for row in range(rows - 1):
        form_divider(parent, form_row_y(height, row) - FORM_ROW_GAP / 2)


# 테두리 버튼·선택 메뉴의 기본 프레임 높이다.
BUTTON_HEIGHT = 26.0


def form_control_y(row_y: float, control_height: float) -> float:
    """입력 줄 안에서 높이 control_height인 컨트롤을 세로 가운데에 놓는 y다."""
    y = row_y + (FORM_ROW - control_height) / 2
    # 테두리 버튼·선택 메뉴는 프레임 아래쪽에 그림자 여백이 있어 그대로 두면 입력칸보다 1pt 떠 보인다.
    return y - 1 if control_height == BUTTON_HEIGHT else y


def form_row_y(height: float, row: int) -> float:
    """높이 height인 설정 카드에서 row번째 입력 줄의 아래쪽 y다."""
    top = height - CARD_PADDING - CARD_TITLE - CARD_TITLE_GAP
    return top - (row + 1) * FORM_ROW - row * FORM_ROW_GAP


# 카드 머리의 아이콘(x=30) 뒤 제목 위치다. 심볼은 19pt 칸보다 작게 그려지므로 칸 끝보다 앞당긴다.
ENTRY_TITLE_X = 50


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
    # 카드가 유리 배경을 덮지 않고 비쳐 보이도록 옅게 칠한다. 어두운 모드는 살짝 더 어둡게,
    # 밝은 모드는 살짝 더 밝게 얹어 유리 위에 한 겹 더 얹은 느낌을 낸다.
    dark = mode == NSAppearanceNameDarkAqua
    return NSColor.colorWithCalibratedWhite_alpha_(0.0 if dark else 1.0, 0.22 if dark else 0.45)


def card(
    parent: NSView,
    x: float,
    y: float,
    width: float,
    height: float,
    *,
    canvas: bool = False,
    input_area: bool = False,
    glass: bool = False,
) -> NSBox:
    """시스템 글자색을 읽기 쉬운 반투명 카드와 얇은 경계로 구분한다."""
    from AppKit import NSBoxCustom, NSNoBorder, NSNoTitle

    view = NSBox.alloc().initWithFrame_(frame(x, y, width, height))
    view.setBoxType_(NSBoxCustom)
    view.setTitlePosition_(NSNoTitle)
    view.setBorderType_(NSNoBorder)
    # 시스템 설정처럼 불투명 화면의 카드는 테두리 없이 바탕색 차이로만 구분한다.
    view.setBorderWidth_(0.7 if glass and not canvas else 0)
    view.setCornerRadius_(0 if canvas else 14 if not input_area else 7)
    color = (
        NSColor.windowBackgroundColor()
        if canvas
        else NSColor.textBackgroundColor()
        if input_area
        else NSColor.controlBackgroundColor()
    )
    # 유리 창의 카드는 뒤가 비치게 옅게 칠하고, 불투명 창(설정·시트)의 카드는 시스템 설정처럼
    # 글자색을 아주 옅게 깔아 배경보다 한 단계만 구분되게 한다.
    view.setFillColor_(
        color.colorWithAlphaComponent_(0)
        if canvas
        else NSColor.colorWithName_dynamicProvider_(None, card_background)
        if glass
        else NSColor.labelColor().colorWithAlphaComponent_(0.035)
    )
    view.setBorderColor_(
        NSColor.labelColor().colorWithAlphaComponent_(0.12) if glass else NSColor.separatorColor()
    )
    parent.addSubview_(view)
    return view
