"""명시적으로 허용한 목록 소스와 칼럼을 개인 필터와 독립적으로 조회한다."""

import asyncio
import json
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Protocol

from src.application.errors import QueryReadError
from src.application.ports import SnapshotReader
from src.application.task_search import TaskSearch, TaskSearchInputError
from src.application.today_metadata import source_metadata
from src.application.work_settings import Settings
from src.domain.models import SourceMapping, Task


class ListAccessError(Exception):
    """인증 실패와 허용되지 않은 조회를 원문 없이 구별한다."""


@dataclass(frozen=True)
class ListGrant:
    """발급 때 동의한 소스 정체성과 칼럼 연결을 고정한다."""

    source_id: str
    binding: str
    fields: tuple[tuple[str, str], ...]


class ListAccessStore(Protocol):
    """기존 개인 토큰과 섞이지 않는 목록 전용 접근 저장 계약이다."""

    def authorize(self, digest: str) -> tuple[ListGrant, ...]:
        """현재 사용·만료·폐기를 확인하고 허용 목록을 반환한다."""
        ...

    def issue(
        self, name: str, days: int | None, grants: tuple[ListGrant, ...]
    ) -> dict[str, object]:
        """허용 범위를 바꿀 수 없는 전용 토큰을 한 번 발급한다."""
        ...

    def tokens(self) -> list[dict[str, object]]:
        """원문과 해시를 제외한 발급·허용 범위 메타데이터를 읽는다."""
        ...

    def revoke(self, identifier: str) -> None:
        """기존 접근을 다음 요청부터 폐기한다."""
        ...


class ListQueryReader(Protocol):
    """HTTP와 stdio가 같은 목록 조회 결과를 사용한다."""

    async def catalog(self, digest: str | None) -> dict[str, object]:
        """호출자에게 허용된 소스와 칼럼만 읽는다."""
        ...

    async def search(self, digest: str | None, payload: object) -> dict[str, object]:
        """호출자가 명시한 조건만 허용한 행에 적용한다."""
        ...


def source_binding(source: SourceMapping, settings: Settings) -> str:
    """사람·화면 설정과 무관하게 실제 원본과 카테고리를 식별한다."""
    category = settings.source_categories[source.source_id]
    return json.dumps(
        [source.provider, source.origin, source.kind, source.round_title_property, category.id]
    )


def list_fields(source: SourceMapping) -> dict[str, tuple[str, str, str]]:
    """원형 업무 값과 표시용 저장 문자열의 차이를 숨기지 않는다."""
    result = {
        "title": ("항목 제목", "text", source.properties["title"]),
        "url": ("원본 링크", "url", "url"),
        "observedAt": ("수집 시각", "instant", "observedAt"),
        "sourceEditedAt": ("원본 수정 시각", "instant", "sourceEditedAt"),
    }
    for key, role, label, kind in (
        ("status", "status", "원본 상태", "text"),
        ("due", "dueDate", "기한", "period"),
        ("assignees", "assignees", "담당자 참조", "people"),
    ):
        if role in source.properties:
            result[key] = (label, kind, source.properties[role])
    for column in source.storage_layout:
        result["column:" + column.id] = (
            column.label,
            column.kind,
            json.dumps([column.value_key, column.kind]),
        )
    return result


class ListQueries:
    """개인 ID·담당자·메인 카드 조건을 조회의 전제로 사용하지 않는다."""

    def __init__(
        self,
        settings: Settings,
        reader: SnapshotReader,
        access: ListAccessStore,
        now: Callable[[], datetime],
        max_age_seconds: int,
    ) -> None:
        """실행 중인 설정과 저장·권한 포트만 받는다."""
        self.settings, self.reader, self.access = settings, reader, access
        self.now, self.max_age_seconds = now, max_age_seconds

    def available(self) -> dict[str, SourceMapping]:
        """일정·휴가를 제외한 현재 카테고리 연결 목록만 선택한다."""
        return {s.source_id: s for s in self.settings.active_sources if s.kind == "task"}

    def describe(self, source: SourceMapping, grant: ListGrant | None = None) -> dict[str, object]:
        """관리자는 선택지를, 조회자는 동의한 칼럼만 확인한다."""
        fields = list_fields(source)
        selected = dict(grant.fields) if grant else {key: item[2] for key, item in fields.items()}
        category = self.settings.source_categories[source.source_id]
        return {
            "sourceId": source.source_id,
            "categoryId": category.id,
            "categoryName": category.name,
            "name": next(
                (s.name or s.title for s in self.settings.display if s.id == source.source_id), ""
            ),
            "fields": [
                {
                    "id": key,
                    "name": fields[key][0],
                    "type": fields[key][1],
                    "available": fields[key][2] == binding,
                }
                if key in fields
                else {"id": key, "available": False}
                for key, binding in selected.items()
            ],
            "rowScope": "all_granted_source_rows",
        }

    def issue(self, payload: object) -> dict[str, object]:
        """소스 전체 행과 정확한 칼럼을 명시적으로 선택한 요청만 발급한다."""
        if not isinstance(payload, dict) or set(payload) != {
            "name",
            "expiresInDays",
            "allRows",
            "sources",
        }:
            raise TaskSearchInputError()
        name, days, entries = payload["name"], payload["expiresInDays"], payload["sources"]
        if (
            not isinstance(name, str)
            or not 1 <= len(name.strip()) <= 60
            or (days is not None and (type(days) is not int or not 1 <= days <= 3650))
            or payload["allRows"] is not True
            or not isinstance(entries, list)
            or not 1 <= len(entries) <= 20
        ):
            raise TaskSearchInputError()
        grants: list[ListGrant] = []
        sources = self.available()
        for entry in entries:
            if not isinstance(entry, dict) or set(entry) != {"sourceId", "fields"}:
                raise TaskSearchInputError()
            identifier, keys = entry["sourceId"], entry["fields"]
            if (
                not isinstance(identifier, str)
                or identifier not in sources
                or not isinstance(keys, list)
                or not 1 <= len(keys) <= 32
                or not all(isinstance(key, str) for key in keys)
            ):
                raise TaskSearchInputError()
            fields = list_fields(sources[identifier])
            if len(set(keys)) != len(keys) or any(key not in fields for key in keys):
                raise TaskSearchInputError()
            grants.append(
                ListGrant(
                    identifier,
                    source_binding(sources[identifier], self.settings),
                    tuple((key, fields[key][2]) for key in keys),
                )
            )
        if len({grant.source_id for grant in grants}) != len(grants):
            raise TaskSearchInputError()
        return self.access.issue(name.strip(), days, tuple(grants))

    async def selected(self, digest: str | None) -> tuple[ListGrant, ...]:
        """익명 호출과 권한 없는 호출을 같은 접근 오류로 거부한다."""
        if digest is None:
            raise ListAccessError()
        return await asyncio.to_thread(self.access.authorize, digest)

    async def catalog(self, digest: str | None) -> dict[str, object]:
        """허용 목록 외 소스의 이름이나 수량을 공개하지 않는다."""
        grants, available = await self.selected(digest), self.available()
        sources = [
            self.describe(available[g.source_id], g)
            for g in grants
            if g.source_id in available
            and g.binding == source_binding(available[g.source_id], self.settings)
        ]
        return {
            "contractVersion": 1,
            "sources": sources,
            "configuration": {
                "status": "ready" if len(sources) == len(grants) else "partial",
                "reasons": [] if len(sources) == len(grants) else ["source_binding_changed"],
            },
        }

    async def search(self, digest: str | None, payload: object) -> dict[str, object]:
        """반환 전 권한·명시 조건을 적용하고 전체 건수 뒤에 페이지를 자른다."""
        grants = await self.selected(digest)
        if not isinstance(payload, dict):
            raise TaskSearchInputError()
        assignee = payload.get("assignee")
        if assignee is not None and (
            not isinstance(assignee, str) or not assignee or len(assignee) > 256
        ):
            raise TaskSearchInputError()
        search = TaskSearch.parse(
            {key: value for key, value in payload.items() if key != "assignee"}
        )
        if search.section != "all":
            raise TaskSearchInputError()
        allowed_ids = {g.source_id for g in grants}
        if search.source_id is not None and search.source_id not in allowed_ids:
            raise ListAccessError()
        available = self.available()
        selected: list[tuple[SourceMapping, ListGrant]] = []
        reasons: list[str] = []
        for grant in grants:
            if search.source_id is not None and grant.source_id != search.source_id:
                continue
            source = available.get(grant.source_id)
            if source is None or grant.binding != source_binding(source, self.settings):
                reasons.append("source_binding_changed")
                continue
            if (
                search.category_id is not None
                and self.settings.source_categories[source.source_id].id != search.category_id
            ):
                continue
            fields = list_fields(source)
            usable = {
                key for key, binding in grant.fields if key in fields and fields[key][2] == binding
            }
            needed = {"title"} if search.criteria.query else set()
            if search.criteria.statuses is not None or search.criteria.include_unset:
                needed.add("status")
            if search.criteria.due_from is not None or search.criteria.due_to is not None:
                needed.add("due")
            if assignee is not None:
                needed.add("assignees")
            # 허용하지 않은 칼럼의 값 존재 여부도 검색 결과로 추론할 수 없게 한다.
            if not needed <= usable:
                raise TaskSearchInputError()
            selected.append((source, grant))
        snapshots = await self.reader.read_sources(
            tuple(
                replace(
                    source,
                    display_columns=tuple(
                        column
                        for column in source.storage_layout
                        if "column:" + column.id in dict(grant.fields)
                    ),
                )
                for source, grant in selected
            )
        )
        observed = self.now()
        metadata = []
        items: list[dict[str, object]] = []
        if len(snapshots) != len(selected):
            raise QueryReadError()
        for (source, grant), snapshot in zip(selected, snapshots, strict=True):
            if snapshot.source_id != source.source_id or snapshot.kind != "task":
                raise QueryReadError()
            metadata.append(source_metadata(snapshot, observed, self.max_age_seconds))
            if not snapshot.mapping_matches:
                continue
            for item in snapshot.items:
                if not isinstance(item, Task):
                    raise QueryReadError()
                if not search.criteria.matches(item) or (
                    assignee is not None and assignee not in item.assignee_ids
                ):
                    continue
                values: dict[str, object] = {
                    "title": item.title,
                    "status": item.status,
                    "due": item.due.payload() if item.due else None,
                    "url": item.url,
                    "assignees": list(item.assignee_ids),
                    "observedAt": item.observed_at,
                    "sourceEditedAt": item.source_edited_at,
                }
                fields = list_fields(source)
                projected: dict[str, object] = {}
                for key, binding in grant.fields:
                    if key not in fields or fields[key][2] != binding:
                        reasons.append("field_binding_changed")
                        projected[key] = {"available": False, "reason": "field_binding_changed"}
                    elif key.startswith("column:"):
                        raw_key = next(
                            column.value_key
                            for column in source.storage_layout
                            if "column:" + column.id == key
                        )
                        raw = item.raw_fields.get(raw_key)
                        if raw is None:
                            reasons.append("raw_field_not_collected")
                        projected[key] = {
                            "available": raw is not None,
                            "representation": "structured",
                            **(raw if raw is not None else {"reason": "raw_field_not_collected"}),
                        }
                    else:
                        projected[key] = {
                            "available": True,
                            "representation": "structured",
                            "value": values[key],
                        }
                items.append({"id": item.id, "sourceId": source.source_id, "fields": projected})
        # 정렬에도 허용하지 않은 제목·기한을 사용하지 않고 원본 키로 안정시킨다.
        items.sort(key=lambda row: (str(row["sourceId"]), str(row["id"])))
        end = search.offset + search.limit
        result: dict[str, object] = {
            "contractVersion": 1,
            "basis": "current_snapshot",
            "timezone": "Asia/Seoul",
            "asOf": observed.isoformat(),
            "scope": "explicit_list_sources",
            "items": items[search.offset : end],
            "totalCount": len(items),
            "limit": search.limit,
            "offset": search.offset,
            "nextOffset": end if end < len(items) else None,
            "truncated": end < len(items),
            "sources": metadata,
            "stale": any(bool(source["stale"]) for source in metadata),
            "configuration": {
                "status": "partial" if reasons else "ready",
                "reasons": sorted(set(reasons)),
            },
        }
        if len(json.dumps(result, ensure_ascii=False).encode()) > 1024 * 1024:
            raise QueryReadError()
        return result
