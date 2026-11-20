"""Notion의 데이터 소스 메타데이터만 관리자 편집용으로 확인한다."""

import asyncio
from collections.abc import Callable
from dataclasses import replace
from urllib.parse import quote

import httpx

from src.application.credentials import CredentialError, CredentialSecret
from src.application.source_settings import (
    SourceColumn,
    SourceSchema,
    SourceSettingsError,
    notion_reference,
)
from src.infrastructure.notion.errors import SourceError
from src.infrastructure.notion.read_api import NotionReadApi
from src.infrastructure.notion.values import array, record, text


class NotionCatalog:
    """선택된 토큰으로 명시적 칼럼 조회만 수행한다."""

    def __init__(self, token: Callable[[], CredentialSecret | None]) -> None:
        """아직 적용 대기 중인 토큰도 명시적 조회에는 사용할 수 있게 한다."""
        self._token = token

    def inspect(self, reference: str, *, rounds: bool) -> tuple[SourceSchema, ...]:
        """임의 네트워크 주소를 호출하지 않고 Notion 참조만 검증한다."""
        identifier = notion_reference(reference)
        try:
            token = self._token()
            if token is None:
                raise SourceSettingsError("연동 → Notion에서 토큰을 먼저 등록하세요.")
            return asyncio.run(self._read(token.value, identifier, rounds))
        except CredentialError:
            raise SourceSettingsError("Notion 토큰을 읽지 못했습니다.") from None
        except (TimeoutError, SourceError) as error:
            code = error.code if isinstance(error, SourceError) else "timeout"
            messages = {
                "unauthorized": "Notion 토큰을 확인하세요.",
                "not_found": "원본 표 주소와 Notion 연결 권한을 확인하세요.",
                "permission_denied": "원본 표를 Notion 연동에 공유하세요.",
                "rate_limited": "Notion 요청 한도에 도달했습니다. 잠시 후 다시 조회하세요.",
                "timeout": "Notion 응답이 늦습니다. 잠시 후 ‘표 불러오기’를 다시 누르세요.",
            }
            raise SourceSettingsError(
                messages.get(code, "Notion 칼럼을 조회하지 못했습니다.")
            ) from None

    async def _read(self, token: str, identifier: str, rounds: bool) -> tuple[SourceSchema, ...]:
        """읽기 제한을 두고 임시 HTTP 클라이언트를 닫는다."""
        async with (
            asyncio.timeout(18),
            httpx.AsyncClient(trust_env=False, follow_redirects=False) as client,
        ):
            return await inspect_columns(NotionReadApi(token, client), identifier, rounds=rounds)


def _catalog_columns(schema: dict[str, object]) -> tuple[SourceColumn, ...]:
    """표 속성에서 편집에 필요한 타입과 상태 선택값만 추린다."""
    properties = record(schema.get("properties"))
    if len(properties) > 256:
        raise SourceSettingsError("칼럼은 한 표에 256개까지 불러올 수 있습니다.")
    result = []
    for name, raw in properties.items():
        prop = record(raw)
        kind = text(prop.get("type"))
        options: tuple[str, ...] = ()
        option_ids: tuple[str, ...] = ()
        if kind in {"status", "select"}:
            option_values = tuple(
                record(value) for value in array(record(prop.get(kind)).get("options"))
            )
            options = tuple(text(option.get("name")) for option in option_values)
            # 일부 ID만 있으면 이름과 위치를 잘못 연결할 수 있어 이름 목록만 제공한다.
            if all(option.get("id") is not None for option in option_values):
                option_ids = tuple(text(option["id"]) for option in option_values)
                if any(not key.strip() for key in option_ids) or len(set(option_ids)) != len(
                    option_ids
                ):
                    raise SourceError("invalid_response")
        result.append(SourceColumn(text(prop.get("id")), name, kind, options, option_ids))
    return tuple(result)


async def inspect_columns(
    api: NotionReadApi, identifier: str, *, rounds: bool
) -> tuple[SourceSchema, ...]:
    """데이터 소스 ID, 원본 DB 또는 페이지 안의 표에서 스키마를 읽는다."""
    try:
        schema = await api.data_schema(identifier)
    except SourceError as error:
        if error.code not in {"not_found", "request_invalid"}:
            raise
    else:
        item = await _catalog_item(api, schema, rounds=rounds)
        if item is None:
            raise SourceSettingsError("부모 항목에서 열 수 있는 하위 표를 찾지 못했습니다.")
        return (item,)

    databases = []
    try:
        databases.append(await api.request(f"/databases/{quote(identifier, safe='')}", None))
    except SourceError as error:
        if error.code not in {"not_found", "request_invalid"}:
            raise
        async for child in api.child_databases(identifier):
            databases.append(await api.request(f"/databases/{quote(child, safe='')}", None))
            if len(databases) > 16:
                raise SourceSettingsError(
                    "이 페이지에는 표가 16개 넘게 있습니다. 연결할 표의 주소를 입력하세요."
                ) from None
    found: list[SourceSchema] = []
    inspected: set[str] = set()
    for database in databases:
        for entry in array(database.get("data_sources")):
            source_id = text(record(entry).get("id"))
            if source_id in inspected:
                continue
            if len(inspected) >= 16:
                raise SourceSettingsError(
                    "데이터 소스가 16개를 넘습니다. 연결할 데이터 소스의 ID를 입력하세요."
                )
            inspected.add(source_id)
            item = await _catalog_item(api, await api.data_schema(source_id), rounds=rounds)
            if item is not None:
                found.append(item)
    if not found:
        raise SourceSettingsError("접근 가능한 표가 없습니다. 원본 표 주소를 입력하세요.")
    return tuple(found)


async def _catalog_item(
    api: NotionReadApi, schema: dict[str, object], *, rounds: bool
) -> SourceSchema | None:
    """차시별 수집 모드에서는 모든 하위 표에서 공통인 칼럼으로 연결한다."""
    identifier = text(schema.get("id"))
    title = "".join(text(record(item).get("plain_text")) for item in array(schema.get("title", [])))
    columns = _catalog_columns(schema)
    if not rounds:
        return SourceSchema(identifier, title or "Notion 표", columns)
    round_titles = tuple(column.name for column in columns if column.kind == "title")
    if not round_titles:
        return None
    child_columns = await _round_columns(api, identifier)
    if child_columns is None:
        return None
    return SourceSchema(
        identifier,
        title or "하위 표 목록",
        child_columns,
        round_titles,
        tuple(c.name for c in columns if c.kind == "number"),
    )


def merge_round_column(previous: SourceColumn, current: SourceColumn) -> SourceColumn:
    """모든 하위 표에서 같은 선택값이 같은 ID를 가질 때만 ID 선택을 제공한다."""
    options = tuple(dict.fromkeys((*previous.options, *current.options)))
    known = dict(zip(previous.options, previous.option_ids, strict=False))
    observed = dict(zip(current.options, current.option_ids, strict=False))
    ids = previous.option_ids if known and known == observed and len(known) == len(options) else ()
    return replace(previous, options=options, option_ids=ids)


async def _round_columns(api: NotionReadApi, identifier: str) -> tuple[SourceColumn, ...] | None:
    """차시별 표 메타데이터를 대조하고 같은 칼럼의 상태 값을 합친다."""
    common: dict[tuple[str, str], SourceColumn] | None = None
    seen: set[str] = set()
    async for raw in api.source_pages(identifier):
        # 차시 문서의 표 위치와 속성만 확인하고 하위 업무 행은 읽지 않는다.
        page_id = text(record(raw).get("id"))
        async for child_id in api.child_source_ids(page_id):
            if child_id in seen:
                continue
            seen.add(child_id)
            columns = _catalog_columns(await api.data_schema(child_id))
            current = {(item.name, item.kind): item for item in columns}
            common = (
                current
                if common is None
                else {
                    key: merge_round_column(item, current[key])
                    for key, item in common.items()
                    if key in current
                }
            )
    return tuple(common.values()) if common is not None else None
