"""기존 업무·QA·회의 링크를 같은 Notion 페이지에서 편집한다."""

from AppKit import NSFont, NSNoBorder, NSScrollView, NSTextField, NSView
from Foundation import NSNotification, NSObject

from src.application.notion_page_links import PageLinksError, page_url
from src.presentation.native_views import button, card, frame, label
from src.presentation.notion_page_links_controller import NotionPageLinksController
from src.presentation.source_help import SourceHelpLabel


class PageLinksActions(NSObject):
    """링크 입력을 계정·토큰 초안과 분리한다."""

    owner: "NotionPageLinksView"

    def controlTextDidChange_(self, notification: NSNotification) -> None:
        """주소 입력은 해당 카드의 수정 상태만 바꾼다."""
        self.owner.controller.edited()
        self.owner.render()

    def save_(self, sender: object) -> None:
        """모든 주소를 검증한 뒤 명시적으로 저장한다."""
        try:
            self.owner.controller.save(
                {key: page_url(field.stringValue()) for key, field in self.owner.fields.items()}
            )
        except PageLinksError:
            self.owner.controller.error = "Notion 페이지 주소를 확인하세요."
        self.owner.render()


class NotionPageLinksView:
    """주소가 많으면 카드 내부만 스크롤하며 초안을 보존한다."""

    def __init__(self, document: NSView, controller: NotionPageLinksController) -> None:
        """계정·인증 카드 아래에 같은 간격과 기본 입력 높이를 적용한다."""
        self.controller = controller
        self._generation = -1
        self._top = document.frame()[1][1] - 254
        self._max_height = max(110.0, self._top - 8)
        self._box = card(document, 16, self._top - 82, 552, 82)
        label(document, "페이지 링크", 28, self._top - 28, 400, size=13, bold=True)
        self._actions = PageLinksActions.alloc().init()
        self._actions.owner = self
        self.save = button(document, "저장", 486, self._top - 31, 70, self._actions, "save:")
        self._scroll = NSScrollView.alloc().initWithFrame_(frame(28, self._top - 70, 528, 34))
        self._scroll.setHasVerticalScroller_(True)
        self._scroll.setAutohidesScrollers_(True)
        self._scroll.setDrawsBackground_(False)
        self._scroll.setBorderType_(NSNoBorder)
        document.addSubview_(self._scroll)
        self.fields: dict[str, NSTextField] = {}
        self.labels: dict[str, SourceHelpLabel] = {}
        self.render()

    def _fill(self) -> None:
        """새 확정 응답에서만 입력을 채워 편집 중 주소가 되돌아가지 않게 한다."""
        state = self.controller.state
        count = len(state.sources) if state else 0
        height = min(self._max_height, 48 + max(1, count) * 34)
        self._box.setFrame_(frame(16, self._top - height, 552, height))
        viewport = height - 48
        self._scroll.setFrame_(frame(28, self._top - height + 12, 528, viewport))
        document_height = max(viewport, max(1, count) * 34)
        content = NSView.alloc().initWithFrame_(frame(0, 0, 528, document_height))
        self.fields = {}
        self.hide_help()
        self.labels = {}
        if not count:
            label(
                content,
                "등록된 페이지 링크 없음" if state else "불러오는 중…",
                0,
                document_height - 27,
                528,
                secondary=True,
            )
        for index, source in enumerate(state.sources if state else ()):
            y = document_height - (index + 1) * 34
            caption = SourceHelpLabel.alloc().initWithFrame_(frame(0, y + 7, 104, 20))
            caption.configureTitle_summary_(source.label, source.summary)
            content.addSubview_(caption)
            self.labels[source.source_id] = caption
            field = NSTextField.textFieldWithString_(source.url or "")
            field.setFont_(NSFont.systemFontOfSize_(13))
            field_height = min(28.0, field.fittingSize()[1])
            field.setFrame_(frame(116, y + (34 - field_height) / 2, 412, field_height))
            field.setPlaceholderString_("Notion 페이지 주소")
            field.setDelegate_(self._actions)
            content.addSubview_(field)
            self.fields[source.source_id] = field
        self._scroll.setDocumentView_(content)
        content.scrollPoint_((0, max(0, document_height - viewport)))

    def hide_help(self) -> None:
        """행을 갱신하기 전에 표시 중인 안내와 예약을 닫는다."""
        for caption in self.labels.values():
            caption.hideHelp()

    def render(self) -> None:
        """저장·조회 중 중복 입력만 막고 미저장 주소는 그대로 유지한다."""
        controller = self.controller
        if self._generation != controller.generation:
            self._fill()
            self._generation = controller.generation
        for field in self.fields.values():
            field.setEnabled_(controller.editable)
        self.save.setEnabled_(controller.editable and controller.dirty)
