"""업무·QA·회의에 공통인 카드·링크·확대·목록 배치다."""

from AppKit import NSView
from Foundation import NSObject

from src.presentation.entry_list import EntryList
from src.presentation.native_views import button, card, frame, label, place_label, symbol


class EntrySection:
    """섹션별 표시값을 분리하고 크기 전환 배치를 한 곳에서 유지한다."""

    def __init__(
        self,
        parent: NSView,
        title: str,
        icon: str,
        target: NSObject,
        open_action: str,
        expand_action: str,
        width: float,
        *,
        title_width: float = 120,
    ) -> None:
        """컨트롤은 한 번만 만들고 이후 프레임과 내용만 바꾼다."""
        self._title_width = title_width
        self.view = NSView.alloc().initWithFrame_(frame(0, 0, width, 0))
        parent.addSubview_(self.view)
        self.card = card(self.view, 16, 0, width - 32, 100)
        self.icon = symbol(self.view, icon, 30, 0)
        self.title = label(self.view, title, 56, 0, title_width, size=13, bold=True)
        self.board = button(
            self.view, "원본 ↗", width - 130, 0, 62, target, open_action, bordered=False
        )
        self.board.setEnabled_(False)
        self.expand = button(
            self.view, "확대", width - 67, 0, 44, target, expand_action, bordered=False
        )
        self.entries = EntryList(self.view, 27, 0, width - 54, 46, target)

    def resize(self, y: float, height: float, width: float) -> None:
        """같은 헤더 중심과 스크롤 여백을 세 영역에 적용한다."""
        previous_size = self.view.frame()[1]
        self.view.setFrame_(frame(0, y, width, height))
        # 다른 카드를 펼쳐 위치만 밀린 경우에는 내부 행과 헤더를 다시 배치하지 않는다.
        if previous_size[0] == width and previous_size[1] == height:
            return
        self.card.setFrame_(frame(16, 0, width - 32, height))
        self.icon.setFrame_(frame(30, height - 30, 19, 19))
        place_label(self.title, 56, height - 30, self._title_width, 20)
        self.board.setFrame_(frame(width - 130, height - 33, 62, 26))
        self.expand.setFrame_(frame(width - 67, height - 33, 44, 26))
        self.entries.resize(27, 12, width - 54, max(1.0, height - 54))
