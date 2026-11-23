"""일반 표와 차시별 하위 표의 Notion 행을 수집해 업무 모델로 변환한다."""

from __future__ import annotations

import asyncio
import re
from copy import copy
from dataclasses import replace
from urllib.parse import quote, unquote

import httpx

from src.application.category_column_types import scope_property_kinds
from src.application.models import CollectedWork
from src.application.source_settings import SourceSchema
from src.domain.display_columns import DisplayColumn
from src.domain.main_list import ListFilter
from src.domain.models import SourceMapping, Task, WorkItem
from src.domain.source_scope import SourceScope, field_parts
from src.infrastructure.notion.catalog import inspect_columns
from src.infrastructure.notion.errors import SourceError
from src.infrastructure.notion.normalization import PageProperties, column_text, normalize_page
from src.infrastructure.notion.read_api import NotionReadApi, SharedNotionReadApi
from src.infrastructure.notion.values import array, record, text


def needs_property_fetch(prop: dict[str, object], kind: str) -> bool:
    """완전한 내장 값은 재사용하고 25명 경계·참조 제목은 전체 속성을 읽는다."""
    if "has_more" in prop and type(prop["has_more"]) is not bool:
        raise SourceError("invalid_response")
    if prop.get("has_more") is True:
        return True
    values = array(prop.get(kind))
    if kind == "people":
        return len(values) >= 25
    # 제목의 제한은 글자 수가 아니라 참조 개수다. 참조가 하나라도 있으면 보수적으로 재조회한다.
    return any(
        record(part).get("type") not in (None, "text") or "mention" in record(part)
        for part in values
    )


def status_options(prop: dict[str, object]) -> tuple[str, ...]:
    """연결된 상태 칼럼에서 행 사용 여부와 관계없이 원본 선택값을 읽는다."""
    kind = text(prop.get("type"))
    if kind not in {"status", "select"}:
        raise SourceError("mapping_invalid")
    options = tuple(
        text(record(option).get("name")) for option in array(record(prop.get(kind)).get("options"))
    )
    if any(not name.strip() for name in options) or len(set(options)) != len(options):
        raise SourceError("invalid_response")
    return options


def resolve_child_scope(
    scope: SourceScope, child_id: str, properties: dict[str, object]
) -> SourceScope:
    """여러 차시에서 공유하는 속성 이름을 현재 하위 표의 실제 ID에 연결한다."""

    def resolve(condition: ListFilter) -> ListFilter:
        """같은 이름의 속성이라도 차시별로 다른 ID를 사용한다."""
        kind, name = field_parts(condition.field)
        identifier = text(record(properties.get(name)).get("id"))
        return replace(condition, field=f"{kind}:{identifier}")

    rules = scope.conditions
    return replace(
        scope,
        source_id=child_id,
        conditions=replace(
            rules,
            filters=tuple(map(resolve, rules.filters)),
            exclusions=tuple(map(resolve, rules.exclusions)),
        ),
    )


class NotionSource:
    """쓰기를 제공하지 않는 Notion 읽기 어댑터다."""

    def __init__(
        self,
        token: str,
        client: httpx.AsyncClient,
        *,
        interval: float = 0.4,
        user_id: str | None = None,
    ) -> None:
        """HTTP와 요청 간격을 주입하고 비밀값은 객체 표현에 노출하지 않는다."""
        self._user_id = user_id
        self._api = NotionReadApi(token, client, interval=interval)

    def for_shared_origin(self, source: SourceMapping) -> NotionSource:
        """같은 표의 여러 매핑에서만 원본 응답을 공유하고 요청 간격은 계속 지킨다."""
        reader = copy(self)
        reader._api = SharedNotionReadApi(self._api)
        return reader

    async def inspect_columns(self, identifier: str, *, rounds: bool) -> tuple[SourceSchema, ...]:
        """기존 비동기 조회 진입점은 같은 요청 간격으로 카탈로그 탐색에 위임한다."""
        return await inspect_columns(self._api, identifier, rounds=rounds)

    async def collect(self, source: SourceMapping, observed_at: str) -> CollectedWork:
        """전체 페이지와 필수 속성을 수집한 경우에만 결과를 반환한다."""
        try:
            async with asyncio.timeout(120):
                if source.round_title_property is not None:
                    return await self._collect_child_tables(source, observed_at)
                options = None
                if source.kind == "task" and (status_id := source.properties.get("status")):
                    schema = await self._api.data_schema(source.origin)
                    properties = PageProperties(record(schema.get("properties")))
                    options = status_options(properties.by_id(status_id))
                return CollectedWork(await self._collect_rows(source, observed_at), options)
        except TimeoutError:
            raise SourceError("timeout") from None

    async def _collect_rows(self, source: SourceMapping, observed_at: str) -> list[WorkItem]:
        """한 표의 정상 행을 모으고 페이지가 중복되면 전체 수집을 중단한다."""
        result: list[WorkItem] = []
        seen: set[str] = set()
        async for raw in self._api.source_pages(source.origin):
            page = record(raw)
            identifier = text(page.get("id"))
            if identifier in seen:
                raise SourceError("pagination_invalid")
            seen.add(identifier)
            properties = PageProperties(record(page.get("properties")))
            await self._fill_columns(identifier, source, properties)
            result.append(
                normalize_page(
                    page, source, observed_at, properties=properties, user_id=self._user_id
                )
            )
        return result

    async def _fill_columns(
        self, identifier: str, source: SourceMapping, properties: PageProperties
    ) -> None:
        """담당자·제목·표시 칼럼 중 페이지네이션이 필요한 값만 추가 조회한다."""
        selected = source.collected_columns()
        # 알림 전용 속성도 노션의 원본 타입과 페이지네이션 규칙으로 빠짐없이 읽는다.
        for rule in source.alert_rules:
            if rule.enabled and (scope := rule.scope_for(source.source_id)) is not None:
                for condition in (*scope.conditions.filters, *scope.conditions.exclusions):
                    _, scope_property = field_parts(condition.field)
                    kind = text(properties.by_id(scope_property).get("type"))
                    if kind not in scope_property_kinds(condition):
                        raise SourceError("mapping_invalid")
                    selected.setdefault(scope_property, kind)
        for role in ("title", "assignees", "participants", "people"):
            property_id = source.properties.get(role)
            if property_id is not None and property_id not in selected:
                selected[property_id] = text(properties.by_id(property_id).get("type"))
        fetched: set[str] = set()
        for property_id, kind in selected.items():
            prop = properties.by_id(property_id)
            if kind == "status_or_select" and prop.get("type") in ("status", "select"):
                continue
            if prop.get("type") != kind:
                raise SourceError("mapping_invalid")
            if kind not in {"title", "rich_text", "people"}:
                continue
            # 역할과 저장 칼럼이 같은 속성을 서로 다른 인코딩으로 가리켜도 상세 페이지는 한 번만 읽는다.
            key = unquote(property_id)
            if key in fetched or not needs_property_fetch(prop, kind):
                continue
            if key in (".", ".."):
                raise SourceError("mapping_invalid")
            encoded = quote(key, safe="")
            path = f"/pages/{quote(identifier, safe='')}/properties/{encoded}"
            values = []
            async for part in self._api.items(path):
                item = record(part)
                if item.get("type") != kind:
                    raise SourceError("invalid_response")
                values.append(item.get(kind))
            prop[kind] = values
            fetched.add(key)

    async def _collect_child_tables(self, source: SourceMapping, observed_at: str) -> CollectedWork:
        """모든 차시의 항목을 읽은 뒤 한 부모 소스의 완전한 결과로 반환한다."""
        if source.kind != "task" or source.round_title_property is None:
            raise SourceError("mapping_invalid")
        schema = await self._api.data_schema(source.origin)
        round_property = record(record(schema.get("properties")).get(source.round_title_property))
        if round_property.get("type") != "title":
            raise SourceError("mapping_invalid")
        round_property_id = text(round_property.get("id"))
        parent = SourceMapping("task", source.source_id, {"title": round_property_id})
        number_id = None
        if source.parent_number_property:
            number = record(record(schema.get("properties")).get(source.parent_number_property))
            if number.get("type") != "number":
                raise SourceError("mapping_invalid")
            number_id = text(number.get("id"))
        result: list[WorkItem] = []
        seen: set[str] = set()
        seen_rounds: set[str] = set()
        seen_children: set[str] = set()
        status_name = source.properties.get("status")
        options: dict[str, None] = {}
        async for raw in self._api.source_pages(source.origin):
            page = record(raw)
            page_id = text(page.get("id"))
            if page.get("object") != "page":
                raise SourceError("invalid_response")
            if page_id in seen_rounds:
                raise SourceError("pagination_invalid")
            seen_rounds.add(page_id)
            # 자식 행에 붙일 차시 제목도 참조·페이지네이션을 끝낸 값만 확정한다.
            parent_props = PageProperties(record(page.get("properties")))
            round_name = ""
            if source.parent_label or source.parent_order in ("suffix", "legacy"):
                await self._fill_columns(page_id, parent, parent_props)
                round_name = column_text(parent_props.by_id(round_property_id), "title")
            # QA 필터는 부모 제목을 차시 이름으로 비교하므로 정렬값과 따로 보존한다.
            # 일반 목록으로 이전한 기존 연결도 종전 차시 값을 유지한다.
            qa_round = (
                round_name
                if source.parent_label
                and (source.section == "qa" or source.parent_order == "legacy")
                else None
            )
            order = None
            if number_id is not None:
                order = column_text(parent_props.by_id(number_id), "number")
            elif source.parent_order == "legacy" and not source.parent_label:
                match = re.search(r"\d+", round_name)
                order = match.group() if match else "0"
            elif source.parent_order == "suffix":
                match = re.search(r"(\d+)\s*차(?:시)?(?:\b|$)", round_name)
                order = match.group(1) if match else None
            async for child_id in self._api.child_source_ids(page_id):
                # 빈 표라도 둘 이상의 차시에 귀속된 원천으로 부모 스냅샷을 교체할 수 없다.
                if child_id in seen_children:
                    raise SourceError("pagination_invalid")
                seen_children.add(child_id)
                child_schema = await self._api.data_schema(child_id)
                properties = record(child_schema.get("properties"))
                if status_name is not None:
                    # 이전 연결에서 자식별 타입이 달라도 상태·선택은 같은 역할로 읽는다.
                    options.update(
                        dict.fromkeys(status_options(record(properties.get(status_name))))
                    )
                mapping = {
                    role: text(record(properties.get(name)).get("id"))
                    for role, name in source.properties.items()
                }
                layout = source.storage_layout
                resolved = tuple(
                    self._resolve_child_column(column, properties) for column in layout
                )
                rules = tuple(rule for rule in source.alert_rules if rule.enabled)
                resolved_rules = tuple(
                    replace(
                        rule,
                        column_id=text(record(properties.get(rule.column_id)).get("id")),
                        source_scopes=tuple(
                            resolve_child_scope(sc, child_id, properties)
                            for sc in rule.source_scopes
                            if sc.source_id == source.source_id
                        )
                        if rule.source_scopes is not None
                        else None,
                    )
                    for rule in rules
                )
                names = {
                    child.value_key: parent.value_key
                    for parent, child in zip(layout, resolved, strict=True)
                }
                child = replace(
                    source,
                    source_id=child_id,
                    origin_id=None,
                    properties=mapping,
                    round_title_property=None,
                    display_columns=resolved if not source.stored_columns else None,
                    role_columns=(),
                    stored_columns=resolved if source.stored_columns else (),
                    alert_rules=resolved_rules,
                    classification_rules=tuple(
                        replace(
                            rule,
                            conditions=tuple(
                                replace(
                                    c,
                                    property_id=text(
                                        record(properties.get(c.property_id)).get("id")
                                    ),
                                )
                                for c in rule.conditions
                            ),
                        )
                        for rule in source.classification_rules
                    ),
                    classification_column=resolved[layout.index(source.classification_column)]
                    if source.classification_column
                    else None,
                )
                for item in await self._collect_rows(child, observed_at):
                    if not isinstance(item, Task) or item.id in seen:
                        raise SourceError("invalid_response")
                    seen.add(item.id)
                    values = {
                        names[identifier]: value
                        for identifier, value in item.display_values.items()
                    }
                    result.append(
                        replace(
                            item,
                            source_id=source.source_id,
                            qa_round=qa_round,
                            parent_label=round_name if source.parent_label else None,
                            parent_order=order,
                            display_values=values,
                            raw_fields={
                                names[key]: value for key, value in item.raw_fields.items()
                            },
                            calendar_periods={
                                names[key]: period for key, period in item.calendar_periods.items()
                            },
                        )
                    )
        # 자식 표가 없으면 빈 선택 목록을 가진 칼럼을 실제로 확인한 것이 아니다.
        return CollectedWork(
            result, tuple(options) if seen_children and status_name is not None else None
        )

    def _resolve_child_column(
        self, column: DisplayColumn, properties: dict[str, object]
    ) -> DisplayColumn:
        """차시 표의 이름을 실제 속성 ID로 해석하고 부모의 공유 칸은 유지한다."""
        property_id = text(record(properties.get(column.property_id)).get("id"))
        return (
            replace(column, origin=property_id)
            if column.origin is not None
            else replace(column, id=property_id)
        )
