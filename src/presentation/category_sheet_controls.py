"""카테고리 설정 탭들이 공유하는 네이티브 입력을 만든다."""

from AppKit import NSMenuItem, NSPopUpButton, NSTextField, NSView

from src.presentation.native_views import frame


def field(
    parent: NSView, value: str, x: float, y: float, width: float, placeholder: str
) -> NSTextField:
    """짧은 값을 읽기 쉬운 같은 너비의 입력으로 배치한다."""
    control = NSTextField.textFieldWithString_(value)
    control.setFrame_(frame(x, y, width, 24))
    control.setPlaceholderString_(placeholder)
    parent.addSubview_(control)
    return control


def set_entries(control: NSPopUpButton, values: list[str]) -> None:
    """선택 목록을 갱신하고 첫 행을 기본으로 둔다."""
    control.removeAllItems()
    for value in values:
        control.menu().addItem_(
            NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(value, None, "")
        )
    control.selectItemAtIndex_(0)


def popup(
    parent: NSView, x: float, y: float, width: float, values: list[str] | None = None
) -> NSPopUpButton:
    """카테고리의 유형이나 표시 기준을 선택하는 기본 macOS 입력이다."""
    control = NSPopUpButton.alloc().initWithFrame_pullsDown_(frame(x, y, width, 26), False)
    if values is not None:
        set_entries(control, values)
    parent.addSubview_(control)
    return control
