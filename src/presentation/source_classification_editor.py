"""수집 소스 한 곳의 원본 값과 사용자 분류 이름을 편집한다."""

from collections.abc import Callable
from weakref import proxy

from AppKit import NSButton, NSScrollView, NSTextField, NSView
from Foundation import NSObject

from src.application.category_column_types import accepts_column_type
from src.application.source_settings import SourceColumn, SourceSettingsError
from src.presentation.category_sheet_controls import field, popup
from src.presentation.native_views import button, frame, label


class ClassificationActions(NSObject):
    """소스 분류 편집기의 칼럼 교체와 행 변경을 전달한다."""

    owner: "SourceClassificationEditor"

    def select_(self, sender: object) -> None:
        """선택한 원본 칼럼의 초안만 연다."""
        self.owner.select()

    def add_(self, sender: object) -> None:
        """스키마에 없는 원본 값도 직접 등록한다."""
        self.owner.edit(None)

    def remove_(self, sender: NSButton) -> None:
        """선택한 행만 제거한다."""
        self.owner.edit(sender.tag())


class SourceClassificationEditor:
    """시트에 적용하기 전까지 칼럼별 입력을 임시로 유지한다."""

    def __init__(
        self,
        page: NSView,
        listing: NSScrollView,
        columns: tuple[SourceColumn, ...],
        saved: dict[str, object],
        same: Callable[[str, str | None], bool],
    ) -> None:
        """조회한 원본과 저장값을 대조해 소스의 분류 규칙을 표시한다."""
        self._columns = tuple(
            col for col in columns if accepts_column_type("notion", "text", col.kind)
        )
        self._actions = ClassificationActions.alloc().init()
        self._actions.owner = proxy(self)
        label(page, "기준 칼럼", 10, 307, 103)
        self._picker = popup(
            page, 119, 302, 503, ["사용 안 함", *(col.name for col in self._columns)]
        )
        self._picker.setTarget_(self._actions)
        self._picker.setAction_("select:")
        label(page, "원본 값", 10, 261, 235)
        label(page, "분류 이름", 269, 261, 190)
        self._add = button(page, "+ 규칙 추가", 509, 255, 114, self._actions, "add:")
        self._list = listing
        label(page, "그 밖의 값", 10, 9, 102)
        self._default = field(page, "", 119, 5, 503, "원본 값 유지")
        self._drafts: dict[str, tuple[list[tuple[str, str]], str]] = {}
        self._rows: list[tuple[NSTextField, NSTextField]] = []
        self._active: SourceColumn | None = None
        self._saved = saved
        self._same = same
        key = saved.get("property")
        match = next(
            (
                i
                for i, col in enumerate(self._columns, 1)
                if isinstance(key, str) and same(col.id, key)
            ),
            0,
        )
        self._missing = bool(saved) and not match
        if self._missing:
            # 스키마에서 사라진 연결은 다른 탭을 저장하는 것만으로 지우지 않는다.
            self._picker.addItemsWithTitles_(["기존 칼럼 · 원본에서 찾을 수 없음"])
            self._picker.selectItemAtIndex_(len(self._columns) + 1)
        else:
            self._picker.selectItemAtIndex_(match)
        if match:
            column = self._columns[match - 1]
            pairs = saved.get("values")
            values = {str(k): str(v) for k, v in pairs.items()} if isinstance(pairs, dict) else {}
            self._drafts[column.id] = (
                [(k, values.get(k, "")) for k in dict.fromkeys((*column.options, *values))],
                str(saved.get("default", "")),
            )
        self.select()

    def select(self) -> None:
        """다른 칼럼에서 편집한 이름과 기본값을 섞지 않는다."""
        if self._active:
            self._drafts[self._active.id] = (self.rows(), self._default.stringValue())
        index = self._picker.indexOfSelectedItem() - 1
        self._active = self._columns[index] if 0 <= index < len(self._columns) else None
        rows, default = (
            self._drafts.get(self._active.id, ([(key, "") for key in self._active.options], ""))
            if self._active
            else ([], "")
        )
        self._default.setStringValue_(default)
        self._default.setEnabled_(self._active is not None)
        self._add.setEnabled_(self._active is not None)
        self.render(rows)

    def rows(self) -> list[tuple[str, str]]:
        """아직 적용하지 않은 텍스트도 다음 조작에 전달한다."""
        return [(a.stringValue(), b.stringValue()) for a, b in self._rows]

    def render(self, rows: list[tuple[str, str]], *, bottom: bool = False) -> None:
        """원본 값과 분류 결과를 한 줄씩 정렬한다."""
        height = max(210, 36 * len(rows) + 8)
        body = NSView.alloc().initWithFrame_(frame(0, 0, 622, height))
        self._rows.clear()
        for i, (key, value) in enumerate(rows):
            y = height - 36 * (i + 1)
            self._rows.append(
                (
                    field(body, key, 10, y, 242, "원본 값"),
                    field(body, value, 269, y, 239, "기본값 적용"),
                )
            )
            button(body, "삭제", 532, y, 78, self._actions, "remove:").setTag_(i)
        self._list.setDocumentView_(body)
        body.scrollPoint_((0, 0 if bottom else height - 210))

    def edit(self, remove: int | None) -> None:
        """행을 변경하고 추가한 행이 보이게 한다."""
        rows = self.rows()
        if remove is None:
            rows.append(("", ""))
        else:
            rows.pop(remove)
        self.render(rows, bottom=remove is None)

    def draft(self) -> dict[str, object]:
        """명시한 원본 값만 저장하고 나머지는 기본 분류를 따르게 한다."""
        if self._active is None:
            if self._missing and self._picker.indexOfSelectedItem() > 0:
                return dict(self._saved)
            return {}
        values: dict[str, str] = {}
        for origin, result in self.rows():
            key, value = origin.strip(), result.strip()
            if not value:
                continue
            if not key or key in values:
                raise SourceSettingsError("분류 규칙의 원본 값을 입력하고 중복을 확인하세요.")
            values[key] = value
        column = self._active
        data: dict[str, object] = {
            "property": column.id,
            "kind": column.kind,
            "name": column.name,
            "values": values,
        }
        old_property = self._saved.get("property")
        if isinstance(old_property, str) and self._same(column.id, old_property):
            data["property"] = old_property
            if "storageId" in self._saved:
                data["storageId"] = self._saved["storageId"]
        if fallback := self._default.stringValue().strip():
            data["default"] = fallback
        return data
