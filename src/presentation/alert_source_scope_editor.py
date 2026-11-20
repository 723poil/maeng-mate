"""알림 규칙 안에서 공통 저장 칼럼과 연결한 표별 원본 조건을 편집한다."""

from collections.abc import Callable, Mapping
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from urllib.parse import unquote
from weakref import proxy

from AppKit import NSButton, NSView
from Foundation import NSObject, NSRunLoop, NSRunLoopCommonModes, NSTimer

from src.application.category_column_types import LEGACY_CATEGORY_TYPES
from src.application.source_settings import SourceSchema, SourceSettingsClient, SourceSettingsError
from src.domain.category_configuration import CategoryColumn
from src.domain.main_list import MainListRules
from src.domain.source_scope import SourceScope, field_parts
from src.domain.work_dates import canonical_identity
from src.presentation.category_sheet_controls import popup, set_entries
from src.presentation.main_list_editor import MainListEditor, OptionValues
from src.presentation.native_views import button, frame, label


@dataclass(frozen=True)
class AlertSource:
    """표의 연결 정보와 기준 데이터에 쓸 수 있는 카테고리 칼럼을 보관한다."""

    id: str
    name: str
    provider: str
    rounds: bool
    columns: frozenset[str]
    property_names: Mapping[str, str] = field(default_factory=dict, repr=False)

    def saved_property(self, key: str) -> CategoryColumn:
        """차시 공통 이름 또는 이 표의 역할 연결에 기록한 원본 속성명을 복원한다."""
        kind, identifier = field_parts(key)
        name = identifier if self.rounds else self.property_names.get(unquote(identifier), "")
        return CategoryColumn(key, name, kind)


class SourceScopeActions(NSObject):
    """대상 표 선택과 비동기 속성 조회 결과를 메인 스레드에서 처리한다."""

    owner: "AlertSourceScopeEditor"

    def mode_(self, sender: object) -> None:
        """모든 소스와 선택한 소스 사이를 전환한다."""
        self.owner.change_mode()

    def select_(self, sender: object) -> None:
        """선택한 표의 속성 조건을 연다."""
        self.owner.show_page()

    def include_(self, sender: object) -> None:
        """현재 표를 알림 대상으로 추가하거나 제외한다."""
        self.owner.include()

    def refresh_(self, sender: object) -> None:
        """현재 원본 표에서 속성과 선택값을 다시 읽는다."""
        self.owner.inspect()

    def poll_(self, sender: object) -> None:
        """완료한 조회만 해당 표의 편집기에 반영한다."""
        self.owner.poll()


class AlertSourceScopeEditor:
    """원본 표마다 독립된 포함·제외 초안을 유지한다."""

    def __init__(
        self,
        view: NSView,
        columns: tuple[CategoryColumn, ...],
        common: MainListRules | None,
        scopes: tuple[SourceScope, ...] | None,
        sources: tuple[AlertSource, ...],
        client: SourceSettingsClient | None,
        changed: Callable[[], None],
        *,
        category_id: str | None = None,
        received_choices: Callable[[dict[str, tuple[str, ...]]], None] | None = None,
    ) -> None:
        """종전 규칙이면 선택했던 표와 실제 비교값을 먼저 보여 준다."""
        self._view, self._client, self._changed = view, client, changed
        self._sources = {source.id: source for source in sources}
        self._original = {item.source_id: item.conditions for item in scopes or ()}
        self._ids = tuple(dict.fromkeys((*self._original, *self._sources)))
        self._included = set(self._original)
        self._editors: dict[str, MainListEditor] = {}
        self._pages: dict[str, NSView] = {}
        self._notes: dict[str, str] = {}
        self._pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="alert-source-columns")
        self._closed = False
        self._received_choices = received_choices
        self._observed: Future[dict[str, tuple[str, ...]]] | None = None
        self._pending: dict[str, Future[tuple[SourceSchema, ...]]] = {}
        self._timer: NSTimer | None = None
        self._actions = SourceScopeActions.alloc().init()
        self._actions.owner = proxy(self)

        height = view.bounds()[1][1]
        label(view, "대상 소스", 0, height - 27, 76, bold=True)
        self.mode = popup(view, 77, height - 31, 185, ["모든 연결 소스", "선택한 소스만"])
        self.mode.setTarget_(self._actions)
        self.mode.setAction_("mode:")
        label(view, "조건 편집", 281, height - 27, 68)
        self.page = popup(view, 349, height - 31, 293)
        self.page.setTarget_(self._actions)
        self.page.setAction_("select:")
        self._included_button = NSButton.checkboxWithTitle_target_action_(
            "이 소스 포함", self._actions, "include:"
        )
        self._included_button.setFrame_(frame(0, height - 61, 111, 24))
        view.addSubview_(self._included_button)
        self._note = label(view, "", 119, height - 57, 411, secondary=True)
        self._retry = button(view, "속성 조회", 533, height - 64, 108, self._actions, "refresh:")
        self._common_page = NSView.alloc().initWithFrame_(frame(0, 0, 642, height - 36))
        view.addSubview_(self._common_page)
        self.common = MainListEditor(self._common_page, columns, common, changed, alert_scope=True)
        label(self._common_page, "공통 저장 칼럼 조건", 0, height - 67, 390, bold=True)
        self._observed_note = label(self._common_page, "", 150, height - 67, 317, secondary=True)
        if client is not None and category_id is not None:
            self._observed = self._pool.submit(client.observed_choices, category_id)
            self._start_polling()
        self.mode.selectItemAtIndex_(int(scopes is not None))
        self.change_mode()

    def _name(self, key: str) -> str:
        """연결이 없어져도 어느 표의 조건을 제거하는지 구분한다."""
        source = self._sources.get(key)
        return source.name if source else f"연결 끊긴 소스 · {key}"

    def _key(self) -> str | None:
        """공통 조건 페이지는 특정 표 ID를 갖지 않는다."""
        index = self.page.indexOfSelectedItem() - 1
        return self._ids[index] if 0 <= index < len(self._ids) else None

    def change_mode(self) -> None:
        """범위 전환은 초안을 보존하며 적용할 때만 최종 선택을 확정한다."""
        limited = self.mode.indexOfSelectedItem() == 1
        self._update_menu()
        if limited and self._ids:
            self.page.selectItemAtIndex_(1)
        self.show_page()

    def _update_menu(self) -> None:
        """조건 페이지를 열기 전에도 소스별 포함 여부를 확인할 수 있게 한다."""
        mode = self.mode.indexOfSelectedItem()
        count = len(self._included)
        set_entries(
            self.mode,
            ["모든 연결 소스", f"선택한 소스만 · {count}개" if count else "선택한 소스만 · 없음"],
        )
        self.mode.selectItemAtIndex_(mode)
        self.mode.setToolTip_("포함한 소스가 없으면 이 규칙으로 알림을 보내지 않습니다.")
        titles = ["공통 저장 칼럼"]
        if mode == 1:
            titles.extend(
                f"{'포함' if key in self._included else '제외'} · {self._name(key)}"
                for key in self._ids
            )
        set_entries(self.page, titles)
        self.page.setEnabled_(len(titles) > 1)

    def show_page(self) -> None:
        """선택한 표에 필요한 메타데이터만 비동기로 조회한다."""
        key = self._key()
        self._common_page.setHidden_(key is not None)
        for item, page in self._pages.items():
            page.setHidden_(item != key or item not in self._included)
        for control in (self._note, self._retry, self._included_button):
            control.setHidden_(key is None)
        if key is None:
            return
        self._included_button.setState_(int(key in self._included))
        if key not in self._pages:
            original = self._original.get(key, MainListRules())
            height = self._view.bounds()[1][1] - 69
            page = NSView.alloc().initWithFrame_(frame(0, 0, 642, height))
            self._view.addSubview_(page)
            self._pages[key] = page
            # 이름을 모르는 경우에도 칼럼 타입과 원본 키를 유지해 조회 실패로 조건을 잃지 않는다.
            source = self._sources.get(key)
            known = tuple(
                source.saved_property(field)
                if source
                else CategoryColumn(field, "", field_parts(field)[0])
                for field in sorted(original.fields)
            )
            self._editors[key] = MainListEditor(
                page, known, original, self._changed, alert_scope=True, source_fields=True
            )
            label(page, "원본 속성 조건", 0, height - 32, 400, bold=True)
            page.setHidden_(key not in self._included)
        if key not in self._notes:
            self.inspect()
        self._show_note(key)

    def _show_note(self, key: str) -> None:
        """조회 상태와 재시도 가능 여부를 현재 표에만 표시한다."""
        message = self._notes[key]
        self._note.setStringValue_(message)
        self._note.setToolTip_(message)
        self._retry.setEnabled_(
            key in self._sources and self._client is not None and key not in self._pending
        )

    def include(self) -> None:
        """표를 잠시 제외했다가 다시 선택해도 작성 중인 조건을 복원한다."""
        key = self._key()
        if key is None:
            return
        if self._included_button.state() == 1:
            self._included.add(key)
        else:
            self._included.discard(key)
        self._pages[key].setHidden_(key not in self._included)
        index = self.page.indexOfSelectedItem()
        self._update_menu()
        self.page.selectItemAtIndex_(index)
        self._changed()

    def inspect(self) -> None:
        """연결된 데이터 소스 ID로만 읽고 차시별 표는 공통 이름을 조회한다."""
        key = self._key()
        if self._closed or key is None or key in self._pending:
            return
        source = self._sources.get(key)
        if source is None:
            self._notes[key] = "연결이 끊겼습니다. 이 소스를 해제하거나 다시 연결하세요."
        elif self._client is None:
            self._notes[key] = "원본 속성을 조회할 수 없습니다."
        else:
            self._notes[key] = "원본 속성을 불러오는 중…"
            self._pending[key] = self._pool.submit(
                self._client.inspect, source.provider, source.id, rounds=source.rounds
            )
            self._start_polling()
        self._show_note(key)

    def _start_polling(self) -> None:
        """실행 중인 조회들이 같은 메인 스레드 타이머를 사용한다."""
        if self._timer is None:
            self._timer = NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
                0.12, self._actions, "poll:", None, True
            )
            NSRunLoop.currentRunLoop().addTimer_forMode_(self._timer, NSRunLoopCommonModes)

    def poll(self) -> None:
        """표를 전환하는 동안 끝난 응답도 그 표의 입력에만 반영한다."""
        if self._closed:
            return
        if self._observed is not None and self._observed.done():
            try:
                choices = self._observed.result()
            except Exception:
                self._observed_note.setStringValue_("수집값 조회 실패 · 직접 입력 가능")
            else:
                self.common.update_observed(choices)
                if self._received_choices is not None:
                    self._received_choices(choices)
            self._observed = None
        for key, future in tuple(self._pending.items()):
            if not future.done():
                continue
            del self._pending[key]
            try:
                schemas = future.result()
                schema = next(
                    (
                        item
                        for item in schemas
                        if canonical_identity(item.id) == canonical_identity(key)
                    ),
                    None,
                )
                if schema is None:
                    raise SourceSettingsError("연결한 원본 표를 찾을 수 없습니다.")
                self._load_fields(key, schema)
            except SourceSettingsError as error:
                self._notes[key] = str(error)
            except Exception:
                self._notes[key] = "원본 속성 조회에 실패했습니다. 다시 조회하세요."
            if self._key() == key:
                self._show_note(key)
        if self._observed is None and not self._pending and self._timer is not None:
            self._timer.invalidate()
            self._timer = None

    def _load_fields(self, key: str, schema: SourceSchema) -> None:
        """일반 표의 ID와 차시별 공통 이름에 맞춰 원본 칼럼과 선택값을 구성한다."""
        columns: list[CategoryColumn] = []
        options: dict[str, OptionValues] = {}
        source = self._sources[key]
        # 같은 속성 ID의 인코딩 표기가 달라도 저장한 조건은 유지한다.
        previous = {
            (kind, unquote(value)): field
            for field in self._original.get(key, MainListRules()).fields
            for kind, value in (field_parts(field),)
        }
        for column in schema.columns:
            kind = LEGACY_CATEGORY_TYPES.get(column.kind)
            if kind is None:
                continue
            if kind == "date":
                kind = "period"
            field = (
                f"{kind}:{column.name}"
                if source.rounds
                else previous.get((kind, unquote(column.id)), f"{kind}:{column.id}")
            )
            columns.append(CategoryColumn(field, column.name, kind))
            if column.kind in {"select", "status"}:
                options[field] = tuple(
                    (value, column.option_ids[i] if column.option_ids else None)
                    for i, value in enumerate(column.options)
                )
        self._editors[key].update_columns(tuple(columns), options)
        self._notes[key] = (
            ("차시별 하위 표의 공통 속성" if source.rounds else "연결한 표의 원본 속성")
            if columns
            else "조건에 사용할 수 있는 원본 속성이 없습니다."
        )

    def draft(self) -> tuple[SourceScope, ...] | None:
        """선택하지 않은 표의 초안은 제외하고 잘못된 조건이 있는 표를 연다."""
        if self.mode.indexOfSelectedItem() == 0:
            return None
        scopes = []
        for index, key in enumerate(self._ids, 1):
            if key not in self._included:
                continue
            editor = self._editors.get(key)
            try:
                conditions = editor.draft() if editor else self._original.get(key)
            except ValueError:
                self.page.selectItemAtIndex_(index)
                self.show_page()
                raise
            scopes.append(SourceScope(key, conditions or MainListRules()))
        return tuple(scopes)

    def common_draft(self) -> MainListRules | None:
        """공통 저장 칼럼 조건의 오류는 해당 페이지에 표시한다."""
        try:
            return self.common.draft()
        except ValueError:
            self.page.selectItemAtIndex_(0)
            self.show_page()
            raise

    def close(self) -> None:
        """시트가 닫히면 완료되지 않은 조회가 화면에 접근하지 않게 한다."""
        self._closed = True
        if self._timer is not None:
            self._timer.invalidate()
            self._timer = None
        self._pending.clear()
        self._pool.shutdown(wait=False, cancel_futures=True)
