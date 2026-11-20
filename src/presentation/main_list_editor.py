"""카테고리 메인 목록의 표시 조건과 정렬 우선순위를 편집한다."""

from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from weakref import proxy

from AppKit import NSButton, NSColor, NSComboBox, NSImage, NSPopUpButton, NSTextField, NSView
from Foundation import NSObject

from src.domain.category_configuration import CategoryColumn
from src.domain.main_list import (
    FIELD_KINDS,
    RELATIVE_DAY_OPERATORS,
    ListFilter,
    ListSort,
    MainListRules,
    operators_for,
)
from src.presentation.category_sheet_controls import (
    field,
    observed_field,
    popup,
    set_observed_choices,
)
from src.presentation.column_setup_sheet import scroll
from src.presentation.native_views import button, card, frame, label

FIELD_NAMES = {
    "title": "제목",
    "status": "상태",
    "qaRound": "QA 차시",
    "date": "마감일·일정 시작일",
    "sourceDate": "마감일·일정 시작일 (원본 순서)",
    "edited": "최종 수정일",
    "completed": "완료 여부",
    "cancelled": "일정 취소 여부",
    "onDay": "오늘 일정",
    "completedTaskAge": "완료 업무 · 마감 후 일수",
    "completedQaAge": "완료 QA · 마지막 수정 후 일수",
    "qaStatus": "QA 상태",
    "completedQaRound": "완료 QA 차시",
    "statusGroup": "업무 분류 (수집 소스)",
    "roundNumber": "QA 차시 번호",
    "overdue": "기한 지남",
    "dueToday": "오늘 마감",
    "sourceOrder": "소스 등록 순서",
    "sourceRowOrder": "소스 안에서의 순서",
}
OPERATOR_NAMES = {
    "eq": "같음",
    "exact": "공백·대소문자도 같음",
    "ne": "같지 않음",
    "contains": "포함",
    "not_contains": "포함하지 않음",
    "lt": "미만",
    "lte": "이하",
    "gt": "초과",
    "gte": "이상",
    "empty": "값이 비어 있음",
    "not_empty": "값이 있음",
    "today": "오늘",
    "not_ended": "기간이 끝나지 않음",
    "past_days": "최근 N일 (오늘 포함)",
    "next_days": "오늘부터 N일",
    "since_days_ago": "N일 전부터",
}
DATE_COMPARISONS = {"lt": "전날까지", "lte": "당일까지", "gt": "다음 날부터", "gte": "당일부터"}
TIME_COMPARISONS = {
    "lt": "해당 시각 전",
    "lte": "해당 시각까지",
    "gt": "해당 시각 후",
    "gte": "해당 시각부터",
}
NO_VALUE = {"empty", "not_empty", "today", "not_ended"}
ROW_HEIGHT = 36
VALUE_HEIGHT = 29


@dataclass
class Choice:
    """메뉴 문구와 독립된 키로 선택 값을 읽는다."""

    control: NSPopUpButton
    keys: tuple[str, ...]

    def selected(self) -> str:
        """현재 선택한 행의 내부 키를 꺼낸다."""
        return self.keys[self.control.indexOfSelectedItem()]


def choice(
    view: NSView,
    x: float,
    y: float,
    width: float,
    options: Mapping[str, str],
    selected: str,
    description: str,
) -> Choice:
    """한글 이름이 같은 칼럼도 고유 키로 구별한다."""
    control = popup(view, x, y, width, list(options.values()))
    keys = tuple(options)
    control.selectItemAtIndex_(keys.index(selected))
    control.setAccessibilityLabel_(description)
    control.setToolTip_(f"{description} · {options[selected]}")
    return Choice(control, keys)


type OptionValues = tuple[tuple[str, str | None], ...]


@dataclass
class OptionInput:
    """표시 이름이 바뀌거나 재사용되어도 원본 선택값의 ID를 보존한다."""

    control: NSPopUpButton
    values: OptionValues
    original: tuple[str, str | None] | None
    initial_index: int

    def selected(self) -> tuple[str, str | None]:
        """현재 표시 이름과 안정적인 원본 ID를 함께 읽는다."""
        index = self.control.indexOfSelectedItem()
        # 자동 조회와 실제 사용자 선택을 구분해 무변경 적용 시 비교 기준을 보존한다.
        if self.original is not None and index == self.initial_index:
            return self.original
        return self.values[index]


@dataclass
class FilterRow:
    """조건 행에 보이는 입력과 오류 시 이동할 위치를 보관한다."""

    column: Choice
    operator: Choice
    value: NSTextField | Choice | OptionInput | None
    y: float
    original: ListFilter

    def read(self) -> ListFilter:
        """입력 도중의 값도 편집 초안에 보존한다."""
        value = ""
        option_id = None
        if isinstance(self.value, OptionInput):
            value, option_id = self.value.selected()
        elif isinstance(self.value, Choice):
            value = self.value.selected()
        elif self.value is not None:
            value = self.value.stringValue()
        field, operator = self.column.selected(), self.operator.selected()
        # 오프라인 편집에서는 그대로 둔 값의 식별자만 이어받는다.
        if field != self.original.field or operator not in {"eq", "ne", "exact"}:
            option_id = None
        elif not isinstance(self.value, OptionInput) and value == self.original.value:
            option_id = self.original.option_id
        return ListFilter(field, operator, value, option_id)


@dataclass
class SortRow:
    """정렬 칼럼과 방향의 순서를 유지한다."""

    column: Choice
    direction: Choice
    y: float
    values: tuple[NSTextField, ...]

    def read(self) -> ListSort:
        """정렬 메뉴의 선택 값을 규칙으로 읽는다."""
        mode = self.direction.selected()
        # 직접 순서를 처음 선택한 순간에도 빈 첫 입력을 남겨 저장 전에 검증한다.
        values = (
            (tuple(control.stringValue() for control in self.values) or ("",))
            if mode == "manual"
            else ()
        )
        return ListSort(self.column.selected(), mode == "desc", values)


class MainListActions(NSObject):
    """네이티브 이벤트를 해당 행이나 설정에 전달한다."""

    owner: "MainListEditor"

    def addFilter_(self, sender: object) -> None:
        """새 필터를 조건 묶음 끝에 추가한다."""
        self.owner.add_filter()

    def addExclusion_(self, sender: object) -> None:
        """숨김 조건 묶음 끝에 새 행을 추가한다."""
        self.owner.add_filter(exclude=True)

    def addSort_(self, sender: object) -> None:
        """가장 낮은 우선순위에 정렬 기준을 추가한다."""
        self.owner.add_sort()

    def filterField_(self, sender: NSPopUpButton) -> None:
        """새 칼럼의 종류에 맞는 조건과 값을 제공한다."""
        self.owner.change_filter(sender.tag(), field_changed=True)

    def operator_(self, sender: NSPopUpButton) -> None:
        """선택한 조건에 필요한 값 입력만 표시한다."""
        self.owner.change_filter(sender.tag(), field_changed=False)

    def exclusionField_(self, sender: NSPopUpButton) -> None:
        """숨김 조건의 새 칼럼에 맞는 연산자를 표시한다."""
        self.owner.change_filter(sender.tag(), field_changed=True, exclude=True)

    def exclusionOperator_(self, sender: NSPopUpButton) -> None:
        """숨김 조건의 새 연산자에 맞는 값 입력을 표시한다."""
        self.owner.change_filter(sender.tag(), field_changed=False, exclude=True)

    def removeFilter_(self, sender: NSButton) -> None:
        """선택한 필터만 제거한다."""
        self.owner.remove_filter(sender.tag())

    def removeExclusion_(self, sender: NSButton) -> None:
        """선택한 숨김 조건만 제거한다."""
        self.owner.remove_filter(sender.tag(), exclude=True)

    def sortField_(self, sender: NSPopUpButton) -> None:
        """사용할 수 있는 나머지 정렬 칼럼을 갱신한다."""
        self.owner.change_sort(sender.tag())

    def sortMode_(self, sender: NSPopUpButton) -> None:
        """직접 정한 순서를 선택하면 개별 값 입력을 펼친다."""
        self.owner.change_sort(sender.tag())

    def addValue_(self, sender: NSButton) -> None:
        """직접 지정한 순서의 끝에 빈 값을 추가한다."""
        self.owner.add_value(sender.tag())

    def valueUp_(self, sender: NSButton) -> None:
        """직접 지정한 값을 한 칸 위로 옮긴다."""
        self.owner.move_value(sender.tag(), -1)

    def valueDown_(self, sender: NSButton) -> None:
        """직접 지정한 값을 한 칸 아래로 옮긴다."""
        self.owner.move_value(sender.tag(), 1)

    def removeValue_(self, sender: NSButton) -> None:
        """선택한 우선순위 값만 목록에서 제거한다."""
        self.owner.remove_value(sender.tag())

    def removeSort_(self, sender: NSButton) -> None:
        """선택한 정렬 기준만 제거한다."""
        self.owner.remove_sort(sender.tag())

    def up_(self, sender: NSButton) -> None:
        """정렬 기준을 한 순위 올린다."""
        self.owner.move_sort(sender.tag(), -1)

    def down_(self, sender: NSButton) -> None:
        """정렬 기준을 한 순위 내린다."""
        self.owner.move_sort(sender.tag(), 1)

    def edited_(self, sender: object) -> None:
        """선택을 바꾸면 이전 입력 오류를 지운다."""
        self.owner.clear_status()

    def controlTextDidChange_(self, notification: object) -> None:
        """값을 고치는 동안 이전 저장 오류를 지운다."""
        self.owner.clear_status()

    def picked_(self, sender: NSPopUpButton) -> None:
        """같은 표시값을 명시적으로 골라도 원본 옵션 ID를 연결한다."""
        self.owner.pick_value(sender)

    def reset_(self, sender: object) -> None:
        """현재 카테고리의 필터와 정렬 초안을 비운다."""
        self.owner.reset()


class MainListEditor:
    """카테고리별 필터와 다중 정렬을 네이티브 폼에서 구성한다."""

    def __init__(
        self,
        page: NSView,
        columns: tuple[CategoryColumn, ...],
        rules: MainListRules | None,
        clear_status: Callable[[], None],
        *,
        alert_scope: bool = False,
        source_fields: bool = False,
    ) -> None:
        """저장 전까지 시트에만 남는 별도 편집 상태를 만든다."""
        self._alert_scope = alert_scope
        self._source_fields = source_fields
        self._observed: dict[str, tuple[str, ...]] = {}
        self._options: Mapping[str, OptionValues] = {}
        self.clear_status = clear_status
        self._actions = MainListActions.alloc().init()
        self._actions.owner = proxy(self)
        self._rules = rules or MainListRules()
        self._columns = columns
        self._last_names = {item.id: item.name for item in columns}
        self._filter_rows: list[FilterRow] = []
        self._exclusion_rows: list[FilterRow] = []
        self._sort_rows: list[SortRow] = []
        self._value_positions: list[tuple[int, int]] = []
        self._match: Choice | None = None

        self._height = page.bounds()[1][1] - 39
        self._clear = button(
            page,
            "칼럼 조건 지우기" if alert_scope else "규칙 모두 지우기",
            479,
            self._height + 7,
            145,
            self._actions,
            "reset:",
        )
        self._clear.setToolTip_(
            "작성한 적용 조건과 제외 조건을 지웁니다."
            if alert_scope
            else "표시 필터, 숨김 조건, 정렬을 모두 지웁니다. 저장 후 앱을 다시 시작하면 적용됩니다."
        )
        self._list = scroll(page, 0, 0, 642, self._height)
        self._render()

    def update_observed(self, choices: dict[str, tuple[str, ...]]) -> None:
        """조건 목록을 다시 만들지 않고 수집한 선택값만 갱신한다."""
        self._observed = choices
        for row in (*self._filter_rows, *self._exclusion_rows):
            if isinstance(row.value, NSComboBox):
                set_observed_choices(row.value, choices.get(row.column.selected(), ()))

    def update_columns(
        self,
        columns: tuple[CategoryColumn, ...],
        options: Mapping[str, OptionValues] | None = None,
    ) -> None:
        """다른 탭의 칼럼 변경을 반영하면서 삭제된 참조는 고칠 수 있게 남긴다."""
        if columns == self._columns and (options is None or self._options == options):
            return
        self._capture()
        self._columns = columns
        if options is not None:
            self._options = options
        self._last_names.update({item.id: item.name for item in columns if item.name})
        self._render()

    def _kinds(self) -> dict[str, str]:
        """저장 초안에서 선택 가능한 모든 칼럼의 값 종류를 모은다."""
        return {
            **({} if self._alert_scope else FIELD_KINDS),
            **{item.id: item.kind for item in self._columns},
        }

    def _field_names(self) -> dict[str, str]:
        """내장 필드와 사용자가 이름 붙인 저장 칼럼을 구분한다."""
        return {
            **({} if self._alert_scope else FIELD_NAMES),
            **{
                item.id: item.name
                if self._source_fields
                else f"저장 칼럼 · {item.name or '이름 없음'}"
                for item in self._columns
                if item.name or not self._source_fields
            },
        }

    def _field_choice(
        self,
        view: NSView,
        x: float,
        y: float,
        width: float,
        names: dict[str, str],
        selected: str,
        description: str,
    ) -> Choice:
        """삭제된 칼럼을 임의로 대체하지 않고 명시적인 재선택을 허용한다."""
        if selected not in names:
            previous = self._last_names.get(selected)
            if self._source_fields and selected in self._kinds():
                # 조회 전 이름 없는 연결은 그 행에만 표시해 다른 조건에서 잘못 고를 수 없게 한다.
                title = "원본 속성 이름 미확인"
            else:
                title = f"삭제된 칼럼 · {previous or '다시 선택'}"
            names = {selected: title, **names}
        return choice(view, x, y, width, names, selected, description)

    def pick_value(self, sender: NSPopUpButton) -> None:
        """직접 조작한 선택값만 현재 원본 이름과 ID로 저장한다."""
        for row in (*self._filter_rows, *self._exclusion_rows):
            if isinstance(row.value, OptionInput) and row.value.control == sender:
                row.value.original = None
                break
        self.clear_status()

    def _capture(self) -> None:
        """화면을 갱신하기 전에 입력 중인 다른 모든 행까지 함께 읽는다."""
        self._rules = MainListRules(
            match=self._match.selected() if self._match is not None else self._rules.match,
            filters=tuple(row.read() for row in self._filter_rows),
            sorts=tuple(row.read() for row in self._sort_rows),
            exclusions=tuple(row.read() for row in self._exclusion_rows),
        )

    def draft(self) -> MainListRules | None:
        """잘못된 행을 보이게 한 뒤 오류를 알리며 기본 설정은 별도 저장하지 않는다."""
        self._capture()
        kinds = self._kinds()
        try:
            self._rules.validate(kinds)
        except ValueError as error:
            # 도메인의 같은 검증으로 행을 찾아 입력한 곳에서 곧바로 고치게 한다.
            for title, rows in (("필터", self._filter_rows), ("숨김 조건", self._exclusion_rows)):
                for index, condition_row in enumerate(rows, 1):
                    condition = condition_row.read()
                    try:
                        condition.validate(kinds.get(condition.field, ""))
                    except ValueError as row_error:
                        self._reveal(condition_row.y)
                        message = (
                            str(row_error)
                            if condition.field in kinds
                            else "다른 칼럼을 고르거나 이 조건을 삭제하세요."
                        )
                        raise ValueError(f"{title} {index} · {message}") from error
            for index, sort in enumerate(self._rules.sorts):
                try:
                    sort.validate(kinds.get(sort.field, ""))
                except ValueError as row_error:
                    row = self._sort_rows[index]
                    error_y = row.y
                    if kinds.get(sort.field) == "text":
                        # 가장 먼저 고쳐야 할 빈 값 또는 두 번째로 등장한 중복 값을 노출한다.
                        seen: set[str] = set()
                        for position, value in enumerate(sort.values):
                            normalized = value.strip().casefold()
                            if not normalized or normalized in seen:
                                error_y = row.values[position].frame()[0][1]
                                break
                            seen.add(normalized)
                    self._reveal(error_y)
                    message = (
                        str(row_error)
                        if sort.field in kinds
                        else "다른 칼럼을 고르거나 이 정렬 기준을 삭제하세요."
                    )
                    raise ValueError(f"정렬 {index + 1} · {message}") from error
            raise
        if not (self._rules.filters or self._rules.exclusions or self._rules.sorts):
            return None
        return self._rules

    def change_sort(self, index: int) -> None:
        """새 정렬 칼럼과 정렬 방식에 필요한 입력만 펼친다."""
        self._capture()
        sorts = list(self._rules.sorts)
        sort = sorts[index]
        if sort.values and self._kinds().get(sort.field) != "text":
            sorts[index] = replace(sort, values=())
            self._rules = replace(self._rules, sorts=tuple(sorts))
        self._render()
        self._reveal(self._sort_rows[index].y)

    def add_value(self, index: int) -> None:
        """구분 문자에 제한 없이 새 우선순위 값을 한 칸 더 입력한다."""
        self._capture()
        sorts = list(self._rules.sorts)
        sort = sorts[index]
        sorts[index] = replace(sort, values=(*sort.values, ""))
        self._rules = replace(self._rules, sorts=tuple(sorts))
        self._render()
        row = self._sort_rows[index]
        self._reveal(row.values[-1].frame()[0][1])

    def move_value(self, tag: int, delta: int) -> None:
        """입력값을 다시 쓰지 않고 선택한 값의 우선순위를 바꾼다."""
        self._capture()
        index, position = self._value_positions[tag]
        other = position + delta
        sorts = list(self._rules.sorts)
        sort = sorts[index]
        values = list(sort.values)
        if not 0 <= other < len(values):
            return
        values[position], values[other] = values[other], values[position]
        sorts[index] = replace(sort, values=tuple(values))
        self._rules = replace(self._rules, sorts=tuple(sorts))
        self._render()
        self._reveal(self._sort_rows[index].values[other].frame()[0][1])

    def remove_value(self, tag: int) -> None:
        """같은 값이 입력 중이어도 선택한 행의 값만 제거한다."""
        self._capture()
        index, position = self._value_positions[tag]
        sorts = list(self._rules.sorts)
        sort = sorts[index]
        sorts[index] = replace(sort, values=sort.values[:position] + sort.values[position + 1 :])
        self._rules = replace(self._rules, sorts=tuple(sorts))
        self._render()

    def reset(self) -> None:
        """다른 탭의 초안은 유지하며 이 탭만 초기화한다."""
        self._rules = MainListRules()
        self._render()

    def _set_conditions(self, conditions: tuple[ListFilter, ...], *, exclude: bool) -> None:
        """편집한 조건 묶음만 교체하고 나머지 초안을 보존한다."""
        if exclude:
            self._rules = replace(self._rules, exclusions=conditions)
        else:
            self._rules = replace(self._rules, filters=conditions)

    def add_filter(self, *, exclude: bool = False) -> None:
        """선택 가능한 칼럼으로 조건을 추가하고 새 행으로 이동한다."""
        self._capture()
        conditions = self._rules.exclusions if exclude else self._rules.filters
        kinds = self._kinds()
        names = self._field_names()
        if not names:
            return
        field = next(iter(names)) if self._alert_scope else "title"
        operator = (
            "exact"
            if field in self._options
            else "contains"
            if kinds[field] == "text"
            else operators_for(kinds[field])[0]
        )
        self._set_conditions(
            (*conditions, ListFilter(field, operator, "예" if kinds[field] == "boolean" else "")),
            exclude=exclude,
        )
        self._render()
        rows = self._exclusion_rows if exclude else self._filter_rows
        self._reveal(rows[-1].y)

    def remove_filter(self, index: int, *, exclude: bool = False) -> None:
        """선택한 행 하나만 지우고 다른 조건의 순서를 유지한다."""
        self._capture()
        rules = self._rules.exclusions if exclude else self._rules.filters
        self._set_conditions(rules[:index] + rules[index + 1 :], exclude=exclude)
        self._render()

    def change_filter(self, index: int, *, field_changed: bool, exclude: bool = False) -> None:
        """바뀐 칼럼이나 연산자가 같은 형식의 값을 받으면 입력을 이어 쓴다."""
        old = (self._rules.exclusions if exclude else self._rules.filters)[index]
        self._capture()
        filters = list(self._rules.exclusions if exclude else self._rules.filters)
        updated = filters[index]
        kinds = self._kinds()
        kind = kinds.get(updated.field)
        operator = updated.operator
        if field_changed and kind and operator not in operators_for(kind):
            operator = operators_for(kind)[0]
        if field_changed and updated.field in self._options:
            operator = "exact"
        value = updated.value
        # 날짜와 일수처럼 입력 의미가 달라질 때만 종전 값을 비운다.
        if (
            operator in NO_VALUE
            or (field_changed and updated.field in self._options)
            or kinds.get(old.field) != kind
            or (old.operator in RELATIVE_DAY_OPERATORS) != (operator in RELATIVE_DAY_OPERATORS)
        ):
            value = ""
        if kind == "boolean" and operator not in NO_VALUE and value not in ("예", "아니요"):
            value = "예"
        filters[index] = ListFilter(
            updated.field,
            operator,
            value,
            updated.option_id
            if operator in {"eq", "exact", "ne"} and value == updated.value
            else None,
        )
        self._set_conditions(tuple(filters), exclude=exclude)
        self._render()

    def add_sort(self) -> None:
        """아직 사용하지 않은 첫 칼럼을 마지막 정렬 기준으로 추가한다."""
        self._capture()
        used = {item.field for item in self._rules.sorts}
        available = next((key for key in self._field_names() if key not in used), None)
        if available is None:
            return
        self._rules = replace(self._rules, sorts=(*self._rules.sorts, ListSort(available)))
        self._render()
        self._reveal(self._sort_rows[-1].y)

    def remove_sort(self, index: int) -> None:
        """정렬 기준을 지우고 나머지 기준의 우선순위를 당긴다."""
        self._capture()
        sorts = self._rules.sorts
        self._rules = replace(self._rules, sorts=sorts[:index] + sorts[index + 1 :])
        self._render()

    def move_sort(self, index: int, delta: int) -> None:
        """두 기준의 우선순위를 교환하고 이동한 행을 화면에 유지한다."""
        self._capture()
        sorts = list(self._rules.sorts)
        other = index + delta
        if not 0 <= other < len(sorts):
            return
        sorts[index], sorts[other] = sorts[other], sorts[index]
        self._rules = replace(self._rules, sorts=tuple(sorts))
        self._render()
        self._reveal(self._sort_rows[other].y)

    def _reveal(self, y: float) -> None:
        """새 행이나 오류 행이 보이지 않을 때 필요한 거리만 스크롤한다."""
        document = self._list.documentView()
        if document is not None:
            origin = self._list.contentView().bounds()[0][1]
            document.scrollPoint_((0, max(0, min(y - 5, max(origin, y + 32 - self._height)))))

    def _listen(self, control: NSButton, action: str, index: int) -> None:
        """행 번호와 네이티브 선택 이벤트를 연결한다."""
        control.setTarget_(self._actions)
        control.setAction_(action)
        control.setTag_(index)

    def _icon(
        self, view: NSView, x: float, y: float, symbol: str, title: str, action: str, index: int
    ) -> NSButton:
        """아이콘의 목적과 행 번호를 툴팁과 보조 기술에 함께 제공한다."""
        control = button(view, "", x, y, 29, self._actions, action, bordered=False)
        control.setImage_(
            NSImage.imageWithSystemSymbolName_accessibilityDescription_(symbol, title)
        )
        control.setContentTintColor_(NSColor.secondaryLabelColor())
        control.setAccessibilityLabel_(title)
        control.setToolTip_(title)
        control.setTag_(index)
        return control

    def _render(self) -> None:
        """조건 수에 맞춰 카드 높이를 바꾸고 읽던 스크롤 위치를 복원한다."""
        old = self._list.documentView()
        offset = (
            0.0
            if old is None
            else max(
                0.0, old.frame()[1][1] - self._height - self._list.contentView().bounds()[0][1]
            )
        )
        filter_height = 59 + ROW_HEIGHT * max(1, len(self._rules.filters))
        # 숨김 조건이 없으면 제목과 추가 버튼만 있는 한 줄로 둔다.
        exclusion_height = 53 + ROW_HEIGHT * len(self._rules.exclusions)
        sort_height = 80 + ROW_HEIGHT * max(1, len(self._rules.sorts))
        sort_height += sum(
            35 + VALUE_HEIGHT * len(sort.values) for sort in self._rules.sorts if sort.values
        )
        if self._alert_scope:
            sort_height = 0
        height = max(self._height, filter_height + exclusion_height + sort_height + 34)
        view = NSView.alloc().initWithFrame_(frame(0, 0, 626, height))
        top = height - 2
        self._clear.setEnabled_(
            bool(self._rules.filters or self._rules.exclusions or self._rules.sorts)
        )
        self._filter_rows.clear()
        self._exclusion_rows.clear()
        self._sort_rows.clear()
        self._value_positions.clear()
        self._match = None
        self._filters_card(view, top, filter_height)
        exclusion_top = top - filter_height - 12
        self._exclusions_card(view, exclusion_top, exclusion_height)
        if not self._alert_scope:
            self._sort_card(view, exclusion_top - exclusion_height - 12, sort_height)
        self._list.setDocumentView_(view)
        view.scrollPoint_((0, max(0, height - self._height - offset)))
        if old is not None:
            self.clear_status()

    def _filters_card(self, view: NSView, top: float, height: float) -> None:
        """조건 결합 방식과 반복 가능한 필터 행을 한 영역에 놓는다."""
        card(view, 0, top - height, 625, height)
        label(
            view,
            "적용 조건" if self._alert_scope else "표시 필터",
            16,
            top - 36,
            101,
            size=14,
            bold=True,
        )
        if len(self._rules.filters) > 1:
            self._match = choice(
                view,
                113,
                top - 39,
                195,
                {"all": "모두 일치", "any": "하나라도 일치"},
                self._rules.match,
                "필터 결합 방식",
            )
            self._listen(self._match.control, "edited:", 0)
        button(
            view,
            "+ 조건 추가" if self._alert_scope else "+ 필터 추가",
            500,
            top - 39,
            111,
            self._actions,
            "addFilter:",
        ).setEnabled_(bool(self._field_names()))
        if not self._rules.filters:
            label(
                view,
                "적용 조건 없음" if self._alert_scope else "표시 필터 없음",
                17,
                top - 73,
                585,
                secondary=True,
            )
        self._condition_rows(view, top, exclude=False)

    def _exclusions_card(self, view: NSView, top: float, height: float) -> None:
        """조건 하나만 맞아도 숨기는 동작을 헤더에서 설명한다."""
        card(view, 0, top - height, 625, height)
        label(
            view,
            "제외 조건" if self._alert_scope else "숨김 조건",
            16,
            top - 36,
            90,
            size=14,
            bold=True,
        )
        label(
            view,
            "조건이 하나라도 맞으면 알림 대상에서 제외합니다."
            if self._alert_scope
            else "조건이 하나라도 맞으면 숨깁니다.",
            110,
            top - 36,
            355,
            secondary=True,
        )
        button(view, "+ 조건 추가", 500, top - 39, 111, self._actions, "addExclusion:").setEnabled_(
            bool(self._field_names())
        )
        self._condition_rows(view, top, exclude=True)

    def _condition_rows(self, view: NSView, top: float, *, exclude: bool) -> None:
        """표시 필터와 숨김 조건에 같은 종류별 입력을 제공한다."""
        names, kinds = self._field_names(), self._kinds()
        conditions = self._rules.exclusions if exclude else self._rules.filters
        rows = self._exclusion_rows if exclude else self._filter_rows
        title = "숨김 조건" if exclude else "필터"
        for index, rule in enumerate(conditions):
            y = top - 78 - index * ROW_HEIGHT
            prefix = f"{title} {index + 1}"
            column = self._field_choice(view, 13, y, 177, names, rule.field, f"{prefix} 칼럼")
            self._listen(column.control, "exclusionField:" if exclude else "filterField:", index)
            kind = kinds.get(rule.field)
            ops = operators_for(kind) if kind is not None else ()
            labels = {
                **OPERATOR_NAMES,
                **(
                    DATE_COMPARISONS
                    if kind in ("date", "period")
                    else TIME_COMPARISONS
                    if kind == "time"
                    else {}
                ),
            }
            options = {key: labels[key] for key in ops}
            if rule.operator not in options:
                options = {
                    rule.operator: f"다시 선택 · {labels.get(rule.operator, '조건')}",
                    **options,
                }
            op = choice(view, 196, y, 149, options, rule.operator, f"{prefix} 조건")
            self._listen(op.control, "exclusionOperator:" if exclude else "operator:", index)
            value = self._value(view, kind, rule, y, prefix)
            rows.append(FilterRow(column, op, value, y, rule))
            self._icon(
                view,
                583,
                y,
                "minus.circle",
                f"{prefix} 삭제",
                "removeExclusion:" if exclude else "removeFilter:",
                index,
            )

    def _value(
        self, view: NSView, kind: str | None, rule: ListFilter, y: float, prefix: str
    ) -> NSTextField | Choice | OptionInput | None:
        """조건에 필요한 값 종류와 입력 형식을 해당 행에서 바로 알려 준다."""
        if rule.operator in NO_VALUE:
            return None
        if rule.field in self._options and rule.operator in {"eq", "exact", "ne"}:
            entries = self._options[rule.field]
            values: list[tuple[str, str | None]] = [("", None), *entries]
            names = ["값 선택", *(name for name, _ in entries)]
            index = next(
                (
                    i
                    for i, (name, key) in enumerate(values)
                    if (key == rule.option_id if rule.option_id else name == rule.value)
                ),
                None,
            )
            if index is None:
                # 삭제 후 같은 이름으로 다시 생성된 옵션도 원래 ID와 구분한다.
                values.append((rule.value, rule.option_id))
                names.append(f"현재 목록에 없는 값 · {rule.value}")
                index = len(values) - 1
            picker = popup(view, 351, y, 222, names)
            picker.selectItemAtIndex_(index)
            picker.setAccessibilityLabel_(f"{prefix} 값")
            self._listen(picker, "picked:", 0)
            return OptionInput(picker, tuple(values), (rule.value, rule.option_id), index)
        if (
            self._alert_scope
            and not self._source_fields
            and kind == "text"
            and rule.operator in {"eq", "exact", "ne"}
        ):
            combo = observed_field(
                view, rule.value, 351, y, 222, self._observed.get(rule.field, ())
            )
            combo.setDelegate_(self._actions)
            combo.setAccessibilityHelp_(f"{prefix} 비교값")
            return combo
        if kind == "boolean":
            options = {"예": "예", "아니요": "아니요"}
            if rule.value not in options:
                options = {rule.value: "값 선택", **options}
            boolean = choice(view, 351, y, 222, options, rule.value, f"{prefix} 값")
            self._listen(boolean.control, "edited:", 0)
            return boolean
        hint = (
            "며칠 전부터 (예: 7)"
            if rule.operator == "since_days_ago"
            else "일수 (오늘 포함)"
            if rule.operator in RELATIVE_DAY_OPERATORS
            else {
                "date": "YYYY-MM-DD",
                "period": "YYYY-MM-DD",
                "time": "HH:MM",
                "number": "숫자 입력",
            }.get(kind or "", "비교할 텍스트")
        )
        control = field(view, rule.value, 353, y + 1, 216, hint)
        control.setAccessibilityHelp_(f"{prefix} 값 · {hint}")
        control.setToolTip_(hint)
        control.setDelegate_(self._actions)
        return control

    def _sort_card(self, view: NSView, top: float, height: float) -> None:
        """사용할 칼럼과 방향을 우선순위 순으로 나열한다."""
        card(view, 0, top - height, 625, height)
        label(view, "정렬 순서", 16, top - 36, 300, size=14, bold=True)
        label(
            view,
            "맨 위 기준으로 먼저 정렬합니다. 값이 같으면 다음 기준을 씁니다.",
            17,
            top - 59,
            586,
            secondary=True,
        )
        used = {item.field for item in self._rules.sorts}
        names = self._field_names()
        add = button(view, "+ 정렬 추가", 500, top - 39, 111, self._actions, "addSort:")
        add.setEnabled_(bool(names.keys() - used))
        if not self._rules.sorts:
            label(
                view,
                "정렬 기준 없음",
                17,
                top - 94,
                585,
                secondary=True,
            )
        y = top - 99
        kinds = self._kinds()
        for index, sort in enumerate(self._rules.sorts):
            rank = f"{index + 1}순위"
            label(view, rank, 17, y + 3, 48, secondary=True)
            available = {
                key: name for key, name in names.items() if key not in used or key == sort.field
            }
            column = self._field_choice(
                view, 70, y, 233, available, sort.field, f"{rank} 정렬 칼럼"
            )
            self._listen(column.control, "sortField:", index)
            modes = {"asc": "오름차순", "desc": "내림차순"}
            if kinds.get(sort.field) == "text":
                modes["manual"] = "사용자 지정 순서"
            elif sort.values:
                modes["manual"] = "다시 선택 · 지정 순서"
            direction = choice(
                view,
                312,
                y,
                142,
                modes,
                "manual" if sort.values else "desc" if sort.descending else "asc",
                f"{rank} 정렬 방향",
            )
            self._listen(direction.control, "sortMode:", index)
            values = self._priority_inputs(view, index, sort.values, y) if sort.values else ()
            self._sort_rows.append(SortRow(column, direction, y, values))
            self._icon(view, 503, y, "chevron.up", f"{rank} 위로 이동", "up:", index).setEnabled_(
                index > 0
            )
            self._icon(
                view, 539, y, "chevron.down", f"{rank} 아래로 이동", "down:", index
            ).setEnabled_(index + 1 < len(self._rules.sorts))
            self._icon(view, 583, y, "minus.circle", f"{rank} 정렬 삭제", "removeSort:", index)
            y -= ROW_HEIGHT + (35 + VALUE_HEIGHT * len(values) if values else 0)

    def _priority_inputs(
        self, view: NSView, index: int, values: tuple[str, ...], top: float
    ) -> tuple[NSTextField, ...]:
        """값마다 독립된 입력을 제공해 쉼표나 기호도 그대로 정렬 값에 넣는다."""
        label(
            view,
            "표시할 순서대로 값을 입력하세요. 나머지 값은 그 뒤에 표시합니다.",
            72,
            top - 28,
            451,
            size=11,
            secondary=True,
        )
        add = button(view, "+ 값 추가", 527, top - 32, 84, self._actions, "addValue:")
        add.setTag_(index)
        add.setAccessibilityLabel_(f"{index + 1}순위 정렬에 값 추가")
        controls: list[NSTextField] = []
        for position, value in enumerate(values):
            y = top - 59 - position * VALUE_HEIGHT
            label(view, str(position + 1), 73, y + 3, 29, secondary=True)
            control = field(
                view,
                value,
                104,
                y,
                379,
                "먼저 표시할 값" if position == 0 else "다음으로 표시할 값",
            )
            control.setAccessibilityHelp_(f"{index + 1}순위 정렬의 {position + 1}번째 값")
            control.setDelegate_(self._actions)
            controls.append(control)
            tag = len(self._value_positions)
            self._value_positions.append((index, position))
            entry = f"{index + 1}순위의 {position + 1}번째 값"
            self._icon(
                view, 503, y, "chevron.up", f"{entry} 위로 이동", "valueUp:", tag
            ).setEnabled_(position > 0)
            self._icon(
                view, 539, y, "chevron.down", f"{entry} 아래로 이동", "valueDown:", tag
            ).setEnabled_(position + 1 < len(values))
            self._icon(
                view, 583, y, "minus.circle", f"{entry} 삭제", "removeValue:", tag
            ).setEnabled_(len(values) > 1)
        return tuple(controls)
