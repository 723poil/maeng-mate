"""수집 소스의 메타데이터 탐색과 JSON 매핑 편집을 분리한다."""

import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Protocol
from urllib.parse import unquote, urlsplit
from uuid import UUID

from src.application.category_column_types import scope_property_kinds
from src.application.category_configuration_upgrade import move_unassigned_settings
from src.application.models import CollectedWork
from src.application.notion_links import notion_link
from src.application.periodic import utc_now
from src.application.ports import WorkSource
from src.application.work_settings import (
    Settings,
    WorkSettingsDocument,
    WorkSettingsError,
    canonical_sources,
    category_bindings,
    category_documents,
    column_bindings_payload,
    empty_work_settings,
    nonblank,
    parse_categories,
    parse_settings,
    read_category,
    source_documents,
)
from src.domain.category_event_alerts import EVENT_ALERT_TRIGGERS, EventAlertSelection
from src.domain.models import SourceMapping, WorkItem
from src.domain.source_alerts import AlertDelivery, is_source_alert_kind
from src.domain.source_display import SourceCategory, SourceDisplay
from src.domain.source_roles import ROLE_TEMPLATES, STATUS
from src.domain.source_scope import field_parts


class SourceSettingsError(ValueError):
    """연결·입력 오류를 원본 응답과 분리한다."""


@dataclass(frozen=True)
class SourceColumn:
    """행을 읽지 않고 편집에 필요한 칼럼과 선택 값을 반환한다."""

    id: str = field(repr=False)
    name: str
    kind: str
    options: tuple[str, ...] = ()
    option_ids: tuple[str, ...] = field(default=(), repr=False)

    def __post_init__(self) -> None:
        """ID가 있는 경우 각 선택값과 유일하게 일대일로 대응해야 한다."""
        if self.option_ids and (
            self.kind not in {"select", "status"}
            or len(self.options) != len(self.option_ids)
            or len(set(self.option_ids)) != len(self.option_ids)
            or any(not value.strip() for value in self.option_ids)
        ):
            raise ValueError("원본 선택값의 ID 연결을 확인하세요.")


@dataclass(frozen=True)
class SourceSchema:
    """선택 가능한 데이터 소스와 매핑에 쓰는 스키마다."""

    id: str = field(repr=False)
    name: str
    columns: tuple[SourceColumn, ...]
    round_titles: tuple[str, ...] = ()
    parent_numbers: tuple[str, ...] = ()


class SourceCatalog(Protocol):
    """프로바이더의 메타데이터 조회를 편집 화면에서 분리한다."""

    def inspect(self, reference: str, *, rounds: bool) -> tuple[SourceSchema, ...]:
        """입력한 참조에서 선택 가능한 스키마를 읽는다."""
        ...


class ProviderSources:
    """예약 실행기가 프로바이더별 수집 방식을 직접 알 필요가 없게 한다."""

    def __init__(self, readers: Mapping[str, WorkSource]) -> None:
        """현재 지원하는 수집 어댑터를 조립부에서 받는다."""
        self._readers = dict(readers)

    async def collect(
        self, source: SourceMapping, observed_at: str
    ) -> CollectedWork | list[WorkItem]:
        """설정에 지정된 어댑터에 정규화를 위임한다."""
        return await self._readers[source.provider].collect(source, observed_at)


class SourceSettingsStore(Protocol):
    """공통 JSON에서 소스 목록만 편집하는 저장 계약이다."""

    def read_validated(self) -> tuple[WorkSettingsDocument, Settings] | None:
        """같은 읽기에서 얻은 설정 문서와 검증된 매핑을 반환한다."""
        ...

    def observed_choices(
        self, category_id: str, now: datetime, max_age_seconds: int
    ) -> dict[str, tuple[str, ...]]:
        """현재 설정과 같은 시점의 정상 스냅샷에서 선택값만 읽는다."""
        ...

    def save_document(
        self,
        revision: int,
        payload: dict[str, object],
        *,
        event_selections: tuple[EventAlertSelection, ...] = (),
    ) -> tuple[WorkSettingsDocument, Settings]:
        """같은 버전에서 저장한 문서와 실제로 확정한 매핑을 반환한다."""
        ...


@dataclass(frozen=True)
class SourceDraft:
    """관리자 화면에 전달하는 버전이 있는 소스 편집값이다."""

    revision: int
    sources: tuple[dict[str, object], ...] = field(repr=False)


@dataclass(frozen=True)
class CategoryDraft:
    """소스 목록과 분리된 카테고리 편집값이다."""

    revision: int
    categories: tuple[SourceCategory, ...]
    event_selections: tuple[EventAlertSelection, ...] = ()


type CollectionDraft = SourceDraft | CategoryDraft


@dataclass(frozen=True)
class SourceSettingsState:
    """저장한 편집값과 수집 재시작 필요 여부다."""

    settings: SourceDraft
    restart_required: bool
    active_display: tuple[SourceDisplay, ...] = ()
    categories: tuple[SourceCategory, ...] = ()

    @property
    def initialized(self) -> bool:
        """저장 문서가 있으면 설정이 준비된 상태다."""
        return self.settings.revision > 0


class SourceSettingsClient(Protocol):
    """관리자 설정이 사용하는 조회·저장·칼럼 탐색이다."""

    def read(self) -> SourceSettingsState:
        """저장 소스를 읽는다."""
        ...

    def observed_choices(self, category_id: str) -> dict[str, tuple[str, ...]]:
        """카테고리의 원본 선택·상태 칼럼에서 수집한 규칙 후보를 읽는다."""
        ...

    def save(self, value: CollectionDraft) -> SourceSettingsState:
        """버전을 확인하고 소스나 카테고리 중 제출한 쪽만 교체한다."""
        ...

    def inspect(self, provider: str, reference: str, *, rounds: bool) -> tuple[SourceSchema, ...]:
        """선택한 프로바이더에서 속성을 조회한다."""
        ...


def notion_reference(value: str) -> str:
    """Notion 주소에서 페이지나 DB UUID만 추출하며 외부 URL은 거부한다."""
    value = value.strip()
    try:
        return str(UUID(value))
    except ValueError:
        pass
    link = notion_link(value)
    if link is not None:
        path = unquote(urlsplit(link).path).rstrip("/")
        match = re.search(
            r"(?<![0-9a-fA-F])([0-9a-fA-F]{32}|[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12})$",
            path,
        )
        if match:
            return str(UUID(match.group(1)))
    raise SourceSettingsError("Notion 주소 또는 데이터 소스 ID를 확인하세요.")


def mapping_signature(settings: Settings | None) -> object:
    """실행 중인 수집과 표시에 필요한 값만 비교한다."""
    if settings is None:
        return (), {}, ()
    presentation = tuple(
        (
            value.id,
            value.key,
            value.color,
            value.sort,
            value.channels,
            value.calendar_visible,
            value.notification_types,
            value.events,
            value.category.configuration.main_list
            if value.category is not None and value.category.configuration is not None
            else None,
        )
        for value in settings.display
    )
    # 전달 문구와 채널은 큐가 저장 시점의 규칙을 읽는다. 수집 매핑에는 영향을 주지 않는다.
    sources = tuple(
        replace(
            source,
            alert_rules=tuple(replace(rule, delivery=None) for rule in source.alert_rules),
            failure_rules=tuple(replace(rule, delivery=None) for rule in source.failure_rules),
        )
        for source in settings.sources
    )
    return sources, settings.completed_statuses, presentation


def validate_columns(mapping: SourceMapping, schema: SourceSchema) -> None:
    """실제 스키마에 맞는 타입과 차시 속성만 저장한다."""
    rounds = mapping.round_title_property is not None
    if rounds and mapping.round_title_property not in schema.round_titles:
        raise SourceSettingsError("부모 제목 칼럼을 다시 선택하세요.")
    if (
        mapping.parent_number_property
        and mapping.parent_number_property not in schema.parent_numbers
    ):
        raise SourceSettingsError("부모 표의 숫자 칼럼을 다시 선택하세요.")
    columns = {column.name if rounds else unquote(column.id): column for column in schema.columns}
    for role in ROLE_TEMPLATES[mapping.kind]:
        value = mapping.properties.get(role.key)
        if value is None and not role.required:
            continue
        if (
            value is None
            and mapping.flexible
            and role.key in {"assignees", "status", "participants"}
        ):
            continue
        column = columns.get((value or "") if rounds else unquote(value or ""))
        if column is None or column.kind not in role.types:
            raise SourceSettingsError(f"‘{role.title}’에 연결할 원본 칼럼을 다시 선택하세요.")
    for display_column in mapping.storage_layout:
        property_id = display_column.property_id
        column = columns.get(property_id if rounds else unquote(property_id))
        if column is None or (
            column.kind not in STATUS
            if display_column.kind == "status_or_select"
            else column.kind != display_column.kind
        ):
            raise SourceSettingsError(
                f"'{display_column.label}'에 연결된 원본 칼럼을 다시 선택하세요."
            )
    for rule in (entry for entry in mapping.alert_rules if entry.enabled):
        column = columns.get(rule.column_id if rounds else unquote(rule.column_id))
        if column is None or column.kind != rule.column_kind:
            raise SourceSettingsError(f"‘{rule.name}’에 사용할 속성을 다시 선택하세요.")


def scope_property_types(source: SourceMapping) -> set[tuple[str, tuple[str, ...]]]:
    """실제로 켜진 규칙의 원본 속성과 필요한 타입을 표별로 모은다."""
    references: set[tuple[str, tuple[str, ...]]] = set()
    for rule in source.alert_rules:
        if not rule.enabled or (scope := rule.scope_for(source.source_id)) is None:
            continue
        for condition in (*scope.conditions.filters, *scope.conditions.exclusions):
            _, identifier = field_parts(condition.field)
            # 차시는 하위 표의 속성 이름을 쓰며 일반 표만 인코딩한 속성 ID를 복원한다.
            key = identifier if source.round_title_property is not None else unquote(identifier)
            references.add((key, scope_property_kinds(condition)))
    return references


class SourceSettingsService:
    """매핑의 DB 저장과 프로바이더 확인 순서를 관리한다."""

    def __init__(
        self,
        store: SourceSettingsStore,
        active: Settings | None,
        catalogs: Mapping[str, SourceCatalog],
        *,
        now: Callable[[], datetime] = utc_now,
        max_age_seconds: int = 1200,
    ) -> None:
        """운영 중인 매핑의 기준값과 현재 지원하는 조회기를 받는다."""
        self._store = store
        self._active = mapping_signature(active)
        self._display = active.display if active else ()
        self._catalogs = dict(catalogs)
        self._now = now
        self._max_age_seconds = max_age_seconds

    def observed_choices(self, category_id: str) -> dict[str, tuple[str, ...]]:
        """일반 설정 응답과 별도로 열린 규칙 편집기의 후보만 조회한다."""
        return self._store.observed_choices(category_id, self._now(), self._max_age_seconds)

    def read(self) -> SourceSettingsState:
        """탭 조회는 외부를 호출하지 않고 설정 JSON만 읽는다."""
        stored = self._store.read_validated()
        if stored is None:
            current = WorkSettingsDocument(0, empty_work_settings())
            return self._state(current, parse_settings(current.payload))
        return self._state(*stored)

    def _state(self, current: WorkSettingsDocument, parsed: Settings) -> SourceSettingsState:
        """현재 실행과 저장된 의미 매핑만 비교한다."""
        pending = mapping_signature(parsed) != self._active
        return SourceSettingsState(
            SourceDraft(current.revision, canonical_sources(source_documents(current.payload))),
            pending,
            self._pending_display(parsed.display, parsed.categories)
            if pending
            else tuple(item for item in parsed.display if item.category is not None),
            parsed.categories,
        )

    def _pending_display(
        self, stored: tuple[SourceDisplay, ...], categories: tuple[SourceCategory, ...]
    ) -> tuple[SourceDisplay, ...]:
        """적용 대기 중에도 활성 카드의 이름·주소와 유효한 알림을 보여 준다."""
        registered = {item.id: item for item in categories}
        sources = {item.id: item for item in stored}
        result = []
        for active in self._display:
            if active.category is None:
                continue
            latest = sources.get(active.id)
            if active.category is not None and active.category.id in registered:
                active = replace(active, category=registered[active.category.id])
            if latest is not None and latest.key == active.key:
                active = replace(active, url=latest.url, name=latest.name, events=latest.events)
            else:
                active = replace(active, events=())
            result.append(active)
        return tuple(result)

    def inspect(self, provider: str, reference: str, *, rounds: bool) -> tuple[SourceSchema, ...]:
        """제공된 프로바이더만 조회할 수 있도록 경로를 고정한다."""
        if provider not in self._catalogs:
            raise SourceSettingsError("이 소스에서는 칼럼을 불러올 수 없습니다.")
        return self._catalogs[provider].inspect(reference, rounds=rounds)

    def save(self, value: CollectionDraft) -> SourceSettingsState:
        """카테고리와 수집 연결을 독립적으로 검증하고 한 문서에 저장한다."""
        stored = self._store.read_validated()
        if stored is None or stored[0].revision != value.revision:
            raise WorkSettingsError("conflict")
        current, existing = stored
        stored_sources = canonical_sources(source_documents(current.payload))
        if isinstance(value, CategoryDraft):
            if value.event_selections or any(
                item.configuration and item.configuration.event_alerts for item in value.categories
            ):
                raise SourceSettingsError(
                    "알림 설정을 다시 열고 기준 데이터와 조건으로 규칙을 등록하세요."
                )
            previous_categories = {item.id: item for item in existing.categories}
            categories = tuple(
                preserve_rule_delivery(item, previous_categories.get(item.id))
                for item in value.categories
            )
            validate_category_names(categories, existing.categories)
            sources = stored_sources
            removed = {item.id for item in existing.categories} - {item.id for item in categories}
            if any(
                item.category is not None and item.category.id in removed
                for item in existing.display
            ):
                raise SourceSettingsError(
                    "연결된 소스를 다른 카테고리로 옮기거나 제거한 뒤 삭제하세요."
                )
            sources = prune_category_links(sources, existing.categories, categories)
        else:
            sources = canonical_sources(list(value.sources))
            categories = move_unassigned_settings(existing, stored_sources, sources)
            if any(
                "categoryId" not in source and source not in stored_sources for source in sources
            ):
                raise SourceSettingsError("수집 소스의 카테고리를 선택하세요.")
            owners = {category.id for category in categories if category.configuration is not None}
            for raw in sources:
                category_id = raw.get("categoryId")
                if (
                    isinstance(category_id, str)
                    and category_id in owners
                    and raw not in stored_sources
                    and raw.keys()
                    & {
                        "displayColumns",
                        "calendarVisible",
                        "color",
                        "alertRules",
                        "notificationChannels",
                        "notificationTypes",
                    }
                ):
                    raise SourceSettingsError(
                        "저장 칼럼·캘린더·알림 설정은 카테고리에서 변경하세요."
                    )
        payload = {
            **current.payload,
            "sources": list(sources),
            "categories": category_documents(categories),
        }
        parsed = parse_settings(payload)
        previous = {source.source_id: source for source in existing.sources}
        old_active = {source.source_id: source for source in existing.active_sources}
        active_ids = {source.source_id for source in parsed.active_sources}
        for source in parsed.sources:
            check_mapping = isinstance(value, SourceDraft) and not same_source_bindings(
                previous.get(source.source_id), source
            )
            rounds = source.round_title_property is not None
            references = scope_property_types(source) if source.source_id in active_ids else set()
            old = old_active.get(source.source_id)
            if references and old is not None and (old.round_title_property is not None) == rounds:
                # 같은 표·탐색 방식의 기존 참조는 이름·문구·비교값만 바꿔도 다시 조회하지 않는다.
                references -= scope_property_types(old)
            if not check_mapping and not references:
                continue
            profiles = self.inspect(source.provider, source.source_id, rounds=rounds)
            schema = next((item for item in profiles if item.id == source.source_id), None)
            if schema is None:
                raise SourceSettingsError("수집할 데이터 소스를 다시 선택하세요.")
            if check_mapping:
                validate_columns(source, schema)
            # 원격 메타데이터는 저장 트랜잭션 전에 읽는다. 저장소가 이후의 버전 경합을 검사한다.
            if references:
                types = {c.name if rounds else unquote(c.id): c.kind for c in schema.columns}
                if any(types.get(key) not in allowed for key, allowed in references):
                    raise SourceSettingsError(
                        "알림 조건에 연결할 원본 칼럼과 타입을 다시 선택하세요."
                    )
        return self._state(*self._store.save_document(value.revision, payload))


def preserve_rule_delivery(
    category: SourceCategory, previous: SourceCategory | None
) -> SourceCategory:
    """이전 클라이언트가 생략한 전달 설정은 보존하고 새 규칙은 채널 없이 시작한다."""
    config = category.configuration
    if config is None:
        return category
    old = previous.configuration if previous else None
    saved: dict[str, AlertDelivery | None] = {}
    if old is not None:
        saved.update((item.rule.id, item.rule.delivery) for item in old.alerts)
        saved.update((event.trigger, event.delivery) for event in old.event_alerts or ())
    empty = AlertDelivery("", ())
    alerts = tuple(
        replace(item, rule=replace(item.rule, delivery=saved.get(item.rule.id, empty)))
        if item.rule.delivery is None
        else item
        for item in config.alerts
    )
    events = (
        tuple(
            replace(event, delivery=saved.get(event.trigger, empty))
            if event.delivery is None
            else event
            for event in config.event_alerts or ()
        )
        if config.event_alerts is not None
        else None
    )
    return replace(category, configuration=replace(config, alerts=alerts, event_alerts=events))


def same_source_bindings(before: SourceMapping | None, after: SourceMapping) -> bool:
    """새로 검증할 원본 연결이 없으면 값 설정이나 연결 해제는 원격 조회 없이 저장한다."""
    return (
        before is not None
        and before.provider == after.provider
        and before.kind == after.kind
        and before.round_title_property == after.round_title_property
        and (
            after.parent_number_property is None
            or before.parent_number_property == after.parent_number_property
        )
        and after.properties.items() <= before.properties.items()
        and source_attribute_types(after) <= source_attribute_types(before)
    )


def source_attribute_types(source: SourceMapping) -> set[tuple[str, str]]:
    """같은 속성이 여러 곳에 연결돼도 타입 검증을 건너뛰지 않는다."""
    return {(column.property_id, column.kind) for column in source.storage_layout} | {
        (rule.column_id, rule.column_kind) for rule in source.alert_rules if rule.enabled
    }


def prune_category_links(
    sources: tuple[dict[str, object], ...],
    previous: tuple[SourceCategory, ...],
    categories: tuple[SourceCategory, ...],
) -> tuple[dict[str, object], ...]:
    """삭제하거나 타입이 바뀐 칸만 연결 해제한다."""
    old = {
        value.id: {item.id: item.kind for item in value.configuration.columns}
        for value in previous
        if value.configuration is not None
    }
    available = {
        value.id: {
            item.id
            for item in value.configuration.columns
            if old.get(value.id, {}).get(item.id, item.kind) == item.kind
        }
        for value in categories
        if value.configuration is not None
    }
    result = []
    for raw in sources:
        group = raw.get("categoryId")
        if isinstance(group, str) and group in available and "columnMappings" in raw:
            links = {
                key: origin
                for key, origin in category_bindings(raw).items()
                if key in available[group]
            }
            result.append({**raw, "columnMappings": column_bindings_payload(links)})
        else:
            result.append(raw)
    return tuple(result)


def validate_category_names(
    categories: tuple[SourceCategory, ...], previous: tuple[SourceCategory, ...]
) -> None:
    """이전 파일에 같은 이름이 있어도 유지하고 새 중복만 막는다."""
    used: dict[str, set[str]] = {}
    for item in categories:
        used.setdefault(item.name.casefold(), set()).add(item.id)
    for name, ids in used.items():
        if len(ids) > 1 and not ids <= {
            item.id for item in previous if item.name.casefold() == name
        }:
            raise SourceSettingsError("이미 등록된 카테고리 이름입니다.")


def draft_payload(value: CollectionDraft) -> dict[str, object]:
    """편집한 목록과 버전만 전송한다."""
    if isinstance(value, CategoryDraft):
        return {
            "revision": value.revision,
            "categories": category_documents(value.categories),
            **(
                {
                    "eventSelections": [
                        {
                            "categoryId": item.category_id,
                            "trigger": item.trigger,
                            "enabled": item.enabled,
                        }
                        for item in value.event_selections
                    ]
                }
                if value.event_selections
                else {}
            ),
        }
    return {"revision": value.revision, "sources": list(value.sources)}


def parse_collection_draft(value: object) -> CollectionDraft:
    """카테고리 요청과 수집 연결 요청을 구분한다."""
    if isinstance(value, dict) and set(value) - {"eventSelections"} == {"revision", "categories"}:
        revision = value["revision"]
        if type(revision) is not int or revision < 0:
            raise SourceSettingsError("카테고리 설정을 확인하세요.")
        try:
            return CategoryDraft(
                revision,
                parse_categories(value["categories"]),
                parse_event_selections(value.get("eventSelections", [])),
            )
        except (ValueError, KeyError):
            raise SourceSettingsError("카테고리 이름을 입력하고 중복을 확인하세요.") from None
    return parse_source_draft(value)


def parse_event_selections(value: object) -> tuple[EventAlertSelection, ...]:
    """사용자가 변경한 카테고리 규칙 스위치만 받는다."""
    if not isinstance(value, list):
        raise ValueError
    result = []
    for item in value:
        if not isinstance(item, dict) or set(item) != {"categoryId", "trigger", "enabled"}:
            raise ValueError
        identifier, trigger = map(nonblank, (item["categoryId"], item["trigger"]))
        if trigger not in EVENT_ALERT_TRIGGERS or type(item["enabled"]) is not bool:
            raise ValueError
        result.append(EventAlertSelection(str(UUID(identifier)), trigger, item["enabled"]))
    if len({(item.category_id, item.trigger) for item in result}) != len(result):
        raise ValueError
    return tuple(result)


def parse_source_draft(value: object) -> SourceDraft:
    """관리자 요청과 응답의 기본 입력 범위를 확인한다."""
    if (
        not isinstance(value, dict)
        or set(value) != {"revision", "sources"}
        or type(value["revision"]) is not int
        or value["revision"] < 0
    ):
        raise SourceSettingsError("수집 소스 설정을 확인하세요.")
    return SourceDraft(value["revision"], tuple(source_documents({"sources": value["sources"]})))


def schema_payload(schema: SourceSchema) -> dict[str, object]:
    """메타데이터만 직렬화한다."""
    return {
        "id": schema.id,
        "name": schema.name,
        "columns": [
            {
                "id": item.id,
                "name": item.name,
                "kind": item.kind,
                "options": list(item.options),
                **({"optionIds": list(item.option_ids)} if item.option_ids else {}),
            }
            for item in schema.columns
        ],
        "roundTitles": list(schema.round_titles),
        "parentNumbers": list(schema.parent_numbers),
    }


def parse_schemas(value: object) -> tuple[SourceSchema, ...]:
    """로컬 응답의 칼럼과 선택값을 같은 계약으로 좁힌다."""
    if not isinstance(value, list) or len(value) > 16:
        raise ValueError
    result = []
    for schema in value:
        if not isinstance(schema, dict):
            raise ValueError
        columns, rounds = schema.get("columns"), schema.get("roundTitles")
        if not isinstance(columns, list) or not isinstance(rounds, list):
            raise ValueError
        numbers = schema.get("parentNumbers", [])
        if not isinstance(numbers, list):
            raise ValueError
        parsed = []
        for column in columns:
            if not isinstance(column, dict) or not isinstance(column.get("options"), list):
                raise ValueError
            ids = column.get("optionIds", [])
            if not isinstance(ids, list):
                raise ValueError
            parsed.append(
                SourceColumn(
                    nonblank(column["id"]),
                    nonblank(column["name"]),
                    nonblank(column["kind"]),
                    tuple(nonblank(item) for item in column["options"]),
                    tuple(nonblank(item) for item in ids),
                )
            )
        result.append(
            SourceSchema(
                nonblank(schema["id"]),
                nonblank(schema["name"]),
                tuple(parsed),
                tuple(nonblank(name) for name in rounds),
                tuple(nonblank(name) for name in numbers),
            )
        )
    return tuple(result)


def display_payload(value: SourceDisplay) -> dict[str, object]:
    """활성 화면의 표시 자료만 전달한다."""
    return {
        "id": value.id,
        "kind": value.kind,
        "section": value.section,
        "name": value.name,
        "color": value.color,
        "sort": value.sort,
        "url": value.url,
        "calendarVisible": value.calendar_visible,
        "flexible": value.flexible,
        "category": {"id": value.category.id, "name": value.category.name}
        if value.category
        else None,
        "events": [
            list(event)
            for event in value.events
            if value.channels != ()
            and (
                is_source_alert_kind(event[0])
                or value.notification_types is None
                or event[0] in value.notification_types
            )
        ],
    }


def parse_active_display(value: object) -> tuple[SourceDisplay, ...]:
    """관리자 응답의 활성 카드 구성을 검증한다."""
    if not isinstance(value, list):
        raise ValueError
    result = []
    for raw in value:
        if not isinstance(raw, dict):
            raise ValueError
        kind = raw.get("kind")
        if kind not in ("task", "meeting", "leave"):
            raise ValueError
        identifier = nonblank(raw["id"])
        name = nonblank(raw["name"]) if raw.get("name") is not None else None
        color = nonblank(raw["color"]) if raw.get("color") is not None else None
        result.append(
            SourceDisplay(
                identifier,
                kind,
                nonblank(raw["section"]),
                name,
                color,
                nonblank(raw["sort"]),
                url=notion_link(raw.get("url")),
                calendar_visible=raw.get("calendarVisible", True) is True,
                category=read_category(raw.get("category")),
                flexible=raw.get("flexible") is True,
                events=parse_display_events(raw.get("events", [])),
            )
        )
    return tuple(result)


def parse_display_events(value: object) -> tuple[tuple[str, str], ...]:
    """활성 카테고리에서 쓸 알림 키와 이름을 확인한다."""
    if not isinstance(value, list):
        raise ValueError
    events: list[tuple[str, str]] = []
    for event in value:
        if not isinstance(event, list) or len(event) != 2:
            raise ValueError
        events.append((nonblank(event[0]), nonblank(event[1])))
    return tuple(events)
