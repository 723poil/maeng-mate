"""실제 출퇴근 입력의 미기록 상태와 시·분 선택을 함께 다룬다."""

from AppKit import NSFont, NSPopUpButton, NSView
from Foundation import NSObject

from src.presentation.native_views import frame


class RecordClockPicker:
    """미기록을 00시와 구분하는 기본 macOS 시·분 선택기다."""

    def __init__(self, parent: NSView, x: float, y: float, actions: NSObject) -> None:
        """두 선택기를 같은 높이·글꼴·간격으로 배치한다."""
        self.hour = self._popup(
            parent, x, y, 100, ["미기록", *[f"{hour:02d}시" for hour in range(24)]], actions
        )
        self.minute = self._popup(
            parent, x + 104, y, 82, [f"{minute:02d}분" for minute in range(60)], actions
        )

    def _popup(
        self, parent: NSView, x: float, y: float, width: float, titles: list[str], actions: NSObject
    ) -> NSPopUpButton:
        """시·분 변경은 같은 입력 콜백으로 전달한다."""
        popup = NSPopUpButton.alloc().initWithFrame_pullsDown_(frame(x, y, width, 28), False)
        popup.addItemsWithTitles_(titles)
        popup.setFont_(NSFont.systemFontOfSize_(13))
        popup.setTarget_(actions)
        popup.setAction_("changed:")
        parent.addSubview_(popup)
        return popup

    def value(self) -> str | None:
        """선택하지 않은 시간을 예상이나 현재 시각으로 채우지 않는다."""
        hour = self.hour.indexOfSelectedItem() - 1
        return f"{hour:02d}:{self.minute.indexOfSelectedItem():02d}" if hour >= 0 else None

    def set_value(self, value: str | None) -> None:
        """서버 확정값을 새 날짜를 읽었을 때만 채운다."""
        if value is None:
            self.hour.selectItemAtIndex_(0)
            self.minute.selectItemAtIndex_(0)
        else:
            hour, minute = map(int, value.split(":"))
            self.hour.selectItemAtIndex_(hour + 1)
            self.minute.selectItemAtIndex_(minute)

    def set_enabled(self, enabled: bool) -> None:
        """미기록일 때는 분만 잠그고 시각을 선택하면 바로 활성화한다."""
        self.hour.setEnabled_(enabled)
        self.minute.setEnabled_(enabled and self.hour.indexOfSelectedItem() > 0)
