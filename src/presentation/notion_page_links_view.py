"""연결한 Notion 표마다 ‘원본 ↗’으로 열 페이지 주소를 편집한다."""

from AppKit import NSFont, NSTextField, NSView
from Foundation import NSNotification, NSObject

from src.application.notion_page_links import PageLinksError, page_url
from src.presentation.native_views import (
    BUTTON_BEZEL_INSET,
    CARD_GAP,
    CARD_PADDING,
    CARD_TITLE,
    CARD_WIDTH,
    FORM_CONTROL_X,
    FORM_LABEL_WIDTH,
    FORM_ROW,
    button,
    card,
    card_height,
    form_dividers,
    form_row_y,
    frame,
    place_label,
)
from src.presentation.notion_credentials_view import TOKEN_HEIGHT, TOKEN_TOP
from src.presentation.notion_page_links_controller import NotionPageLinksController
from src.presentation.runtime_settings_form import card_title, form_field, row_caption
from src.presentation.source_help import SourceHelpLabel

# 링크 카드는 토큰 카드 바로 아래에 온다.
LINKS_TOP = TOKEN_TOP + TOKEN_HEIGHT + CARD_GAP
SAVE_WIDTH = 88.0


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
    """표가 많으면 카드와 함께 페이지 문서를 늘려 초안을 보존한다."""

    def __init__(self, document: NSView, controller: NotionPageLinksController) -> None:
        """토큰 카드 아래에 표마다 한 줄씩 주소 입력을 놓는다."""
        self.controller = controller
        self._generation = -1
        self._document = document
        self._section = NSView.alloc().initWithFrame_(frame(CARD_PADDING, 0, CARD_WIDTH, 0))
        document.addSubview_(self._section)
        self._actions = PageLinksActions.alloc().init()
        self._actions.owner = self
        # 하단 공통 저장과 구분되도록 이름에 대상을 붙이고, 줄을 다시 그려도 같은 버튼을 쓴다.
        self.save = button(self._section, "링크 저장", 0, 0, SAVE_WIDTH, self._actions, "save:")
        self.fields: dict[str, NSTextField] = {}
        self.labels: dict[str, SourceHelpLabel] = {}
        self.render()

    def _resize_document(self, content: float) -> None:
        """문서 높이를 내용에 맞추고 위쪽 카드와 보던 위치를 그대로 둔다."""
        document = self._document
        clip = document.superview()
        old_height = document.frame()[1][1]
        viewport = clip.bounds()[1][1] if clip is not None else old_height
        old_origin = clip.bounds()[0][1] if clip is not None else 0.0
        height = max(viewport, content)
        delta = height - old_height
        if not delta:
            return
        document.setFrameSize_((document.frame()[1][0], height))
        # 페이지의 카드는 모두 문서 위쪽 기준이므로 늘어난 만큼 함께 올려 위 여백을 유지한다.
        for view in document.subviews():
            (x, y), (width, view_height) = view.frame()
            view.setFrame_(frame(x, y + delta, width, view_height))
        document.scrollPoint_((0, max(0, min(height - viewport, old_origin + delta))))

    def _fill(self) -> None:
        """새 확정 응답에서만 입력을 채워 편집 중 주소가 되돌아가지 않게 한다."""
        state = self.controller.state
        sources = state.sources if state else ()
        rows = max(1, len(sources))
        height = card_height(rows)
        self._resize_document(LINKS_TOP + height + CARD_PADDING)
        self.fields = {}
        self.hide_help()
        self.labels = {}
        section = self._section
        section.setSubviews_([])
        section.setFrame_(
            frame(
                CARD_PADDING,
                self._document.frame()[1][1] - LINKS_TOP - height,
                CARD_WIDTH,
                height,
            )
        )
        card(section, 0, 0, CARD_WIDTH, height)
        form_dividers(section, height, rows)
        card_title(section, "페이지 링크", height)
        self.save.setFrame_(
            frame(
                CARD_WIDTH - CARD_PADDING - SAVE_WIDTH + BUTTON_BEZEL_INSET,
                height - CARD_PADDING - CARD_TITLE - 3,
                SAVE_WIDTH,
                26,
            )
        )
        section.addSubview_(self.save)
        if not sources:
            row_caption(
                section, "연결한 표가 없습니다." if state else "불러오는 중…", form_row_y(height, 0)
            )
        for index, source in enumerate(sources):
            row = form_row_y(height, index)
            caption = SourceHelpLabel.alloc().initWithFrame_(
                frame(CARD_PADDING, row, FORM_LABEL_WIDTH - 8, 20)
            )
            caption.configureTitle_summary_(source.label, source.summary)
            caption.setFont_(NSFont.systemFontOfSize_(12))
            place_label(caption, CARD_PADDING, row, FORM_LABEL_WIDTH - 8, FORM_ROW)
            section.addSubview_(caption)
            self.labels[source.source_id] = caption
            field = form_field(
                section, row, CARD_WIDTH - CARD_PADDING - FORM_CONTROL_X, "Notion 페이지 주소"
            )
            field.setStringValue_(source.url or "")
            field.setDelegate_(self._actions)
            self.fields[source.source_id] = field

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
