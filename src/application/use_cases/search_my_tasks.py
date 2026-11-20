"""현재 본인 업무를 제한 전에 검색하고 조회 근거를 함께 반환한다."""

from collections.abc import Callable, Iterator
from dataclasses import replace
from datetime import datetime
from heapq import nsmallest

from src.application.personal_work import (
    checked_query_result,
    raw_task_sort_key,
    read_personal_sources,
    relevant_items,
)
from src.application.ports import SnapshotReader
from src.application.task_search import TaskSearch
from src.application.today_models import TodayContext
from src.domain.models import Task
from src.domain.policies import RelevancePolicy


def personal_task_context(context: TodayContext, search: TaskSearch | None = None) -> TodayContext:
    """표 선택은 본인 담당 칼럼이 연결된 범위 안에서만 결과를 좁힌다."""
    sources = tuple(
        source
        for source in context.sources
        if context.user_id
        and source.kind == "task"
        and source.has_person_scope
        and (
            search is None
            or (
                (search.section == "all" or source.section == search.section)
                and (
                    search.category_id is None
                    or (
                        (category := context.source_categories.get(source.source_id)) is not None
                        and category.id == search.category_id
                    )
                )
                and (search.source_id is None or search.source_id == source.source_id)
            )
        )
    )
    # 관리자 카드의 숨김·표시 칼럼 및 표 전체 권한은 개인 도구에 전달하지 않는다.
    return replace(context, sources=sources, desktop_areas={}, main_cards={}, display_columns={})


async def list_my_task_statuses(
    reader: SnapshotReader,
    context: TodayContext,
    policy: RelevancePolicy,
    now: Callable[[], datetime],
) -> dict[str, object]:
    """업무 행을 보내지 않고 표별 상태 이름과 미지정 항목 유무, 수집 근거를 반환한다."""
    selected = personal_task_context(context)
    selection = await read_personal_sources(None, reader, selected, policy, now)
    sources = []
    for batch in selection.batches:
        source, snapshot = batch.source, batch.snapshot
        tracks_status = "status" in source.properties
        usable = tracks_status and snapshot.mapping_matches and snapshot.last_success_at is not None
        options = snapshot.status_options if usable else None
        # 옛 스냅샷에 스키마가 없을 때도 다른 담당자의 업무 상태를 노출하지 않는다.
        names = dict.fromkeys(options or ())
        has_unset = False
        # 목록 조회에는 제목·URL·날짜를 직렬화하지 않고 같은 권한을 통과한 상태만 읽는다.
        for item in relevant_items(snapshot, selected.user_id, policy):
            if isinstance(item, Task):
                if item.status is None:
                    has_unset = True
                elif usable:
                    names[item.status] = None
        sources.append(
            {
                **batch.metadata,
                "name": selected.source_names.get(source.source_id),
                "section": source.section,
                **selected.category_fields(source),
                "tracksStatus": tracks_status,
                "statuses": list(names),
                "hasUnspecifiedStatus": has_unset,
                "statusOptionsBasis": "schema"
                if options is not None
                else "observed"
                if usable
                else "unavailable",
            }
        )
    return checked_query_result({**selection.metadata, "sources": sources})


async def search_my_tasks(
    search: TaskSearch,
    reader: SnapshotReader,
    context: TodayContext,
    policy: RelevancePolicy,
    now: Callable[[], datetime],
) -> dict[str, object]:
    """개인 소스 범위를 유지하고 모든 일치 항목을 찾은 뒤 페이지를 자른다."""
    selected = personal_task_context(context, search)
    selection = await read_personal_sources(
        None, reader, selected, policy, now, task_filter=search.criteria.matches
    )
    rows = (row for _, _, row in selection.rows(selected))
    end = search.offset + search.limit
    total = 0

    def counted_rows() -> Iterator[dict[str, object]]:
        """앞 페이지를 찾는 동안에도 범위에 맞는 전체 업무 수를 센다."""
        nonlocal total
        for row in rows:
            total += 1
            yield row

    if search.offset >= selection.max_rows:
        total = sum(1 for _ in rows)
        tasks = []
    elif end * 2 >= selection.max_rows:
        ordered = sorted(rows, key=raw_task_sort_key)
        total = len(ordered)
        tasks = ordered[search.offset : end]
    else:
        tasks = nsmallest(end, counted_rows(), key=raw_task_sort_key)[search.offset :]
    next_offset = search.offset + len(tasks)
    return checked_query_result(
        {
            **selection.metadata,
            "truncated": len(tasks) < total,
            "tasks": tasks,
            "totalCount": total,
            "limit": search.limit,
            "offset": search.offset,
            "nextOffset": next_offset if next_offset < total else None,
        }
    )
