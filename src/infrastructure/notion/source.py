"""일반 표와 차시별 하위 표의 Notion 행을 수집해 업무 모델로 변환한다."""

import asyncio
from dataclasses import replace
from urllib.parse import quote, unquote

import httpx

from src.application.source_settings import SourceSchema
from src.domain.display_columns import DisplayColumn
from src.domain.models import SourceMapping, Task, WorkItem
from src.infrastructure.notion.catalog import inspect_columns
from src.infrastructure.notion.errors import SourceError
from src.infrastructure.notion.normalization import normalize_page, property_by_id
from src.infrastructure.notion.read_api import NotionReadApi
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


class NotionSource:
    """쓰기를 제공하지 않는 Notion 읽기 어댑터다."""

    def __init__(self, token: str, client: httpx.AsyncClient, *, interval: float = 0.4) -> None:
        """HTTP와 요청 간격을 주입하고 비밀값은 객체 표현에 노출하지 않는다."""
        self._api = NotionReadApi(token, client, interval=interval)

    async def inspect_columns(self, identifier: str, *, rounds: bool) -> tuple[SourceSchema, ...]:
        """기존 비동기 조회 진입점은 같은 요청 간격으로 카탈로그 탐색에 위임한다."""
        return await inspect_columns(self._api, identifier, rounds=rounds)

    async def collect(self, source: SourceMapping, observed_at: str) -> list[WorkItem]:
        """전체 페이지와 필수 속성을 수집한 경우에만 결과를 반환한다."""
        try:
            async with asyncio.timeout(120):
                if source.round_title_property is not None:
                    return await self._collect_qa_rounds(source, observed_at)
                return await self._collect_rows(source, observed_at)
        except TimeoutError:
            raise SourceError("timeout") from None

    async def _collect_rows(self, source: SourceMapping, observed_at: str) -> list[WorkItem]:
        """한 표의 정상 행을 모으고 페이지가 중복되면 전체 수집을 중단한다."""
        result: list[WorkItem] = []
        seen: set[str] = set()
        async for raw in self._api.source_pages(source.source_id):
            page = record(raw)
            identifier = text(page.get("id"))
            if identifier in seen:
                raise SourceError("pagination_invalid")
            seen.add(identifier)
            properties = record(page.get("properties"))
            await self._fill_columns(identifier, source, properties)
            result.append(normalize_page(page, source, observed_at))
        return result

    async def _fill_columns(
        self, identifier: str, source: SourceMapping, properties: dict[str, object]
    ) -> None:
        """담당자·제목·표시 칼럼 중 페이지네이션이 필요한 값만 추가 조회한다."""
        selected = source.collected_columns()
        for role in ("title", "assignees", "participants", "people"):
            if (property_id := source.properties.get(role)) is not None:
                selected.setdefault(
                    property_id, text(property_by_id(properties, property_id).get("type"))
                )
        for property_id, kind in selected.items():
            prop = property_by_id(properties, property_id)
            if prop.get("type") != kind:
                raise SourceError("mapping_invalid")
            if kind not in {"title", "rich_text", "people"} or not needs_property_fetch(prop, kind):
                continue
            encoded = quote(unquote(property_id), safe="")
            if unquote(encoded) in (".", ".."):
                raise SourceError("mapping_invalid")
            path = f"/pages/{quote(identifier, safe='')}/properties/{encoded}"
            values = []
            async for part in self._api.items(path):
                item = record(part)
                if item.get("type") != kind:
                    raise SourceError("invalid_response")
                values.append(item.get(kind))
            prop[kind] = values

    async def _collect_qa_rounds(self, source: SourceMapping, observed_at: str) -> list[WorkItem]:
        """모든 차시의 항목을 읽은 뒤 한 부모 소스의 완전한 결과로 반환한다."""
        if source.kind != "task" or source.round_title_property is None:
            raise SourceError("mapping_invalid")
        schema = await self._api.request(f"/data_sources/{quote(source.source_id, safe='')}", None)
        round_property = record(record(schema.get("properties")).get(source.round_title_property))
        round_property_id = text(round_property.get("id"))
        result: list[WorkItem] = []
        seen: set[str] = set()
        async for raw in self._api.source_pages(source.source_id):
            page = record(raw)
            title = property_by_id(record(page.get("properties")), round_property_id)
            if title.get("type") != "title":
                raise SourceError("mapping_invalid")
            round_name = "".join(
                text(record(part).get("plain_text")) for part in array(title.get("title"))
            )
            async for child_id in self._api.child_source_ids(text(page.get("id"))):
                child_schema = await self._api.data_schema(child_id)
                properties = record(child_schema.get("properties"))
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
                    replace(rule, column_id=text(record(properties.get(rule.column_id)).get("id")))
                    for rule in rules
                )
                names = {
                    child.value_key: parent.value_key
                    for parent, child in zip(layout, resolved, strict=True)
                }
                child = replace(
                    source,
                    source_id=child_id,
                    properties=mapping,
                    round_title_property=None,
                    display_columns=resolved if not source.stored_columns else None,
                    stored_columns=resolved if source.stored_columns else (),
                    alert_rules=resolved_rules,
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
                            qa_round=round_name,
                            display_values=values,
                            calendar_periods={
                                names[key]: period for key, period in item.calendar_periods.items()
                            },
                        )
                    )
        return result

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
