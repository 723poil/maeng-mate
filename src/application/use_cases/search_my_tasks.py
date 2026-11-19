"""현재 본인 업무를 제한 전에 검색하고 조회 근거를 함께 반환한다."""

from collections.abc import Callable, Iterator
from dataclasses import replace
from datetime import datetime
from functools import partial
from heapq import nsmallest

from src.application.personal_work import checked_query_result, read_personal_sources, task_sort_key
from src.application.ports import SnapshotReader
from src.application.task_search import TaskSearch
from src.application.today_models import TodayContext
from src.domain.models import Task
from src.domain.policies import RelevancePolicy


async def search_my_tasks(
    search: TaskSearch,
    reader: SnapshotReader,
    context: TodayContext,
    policy: RelevancePolicy,
    now: Callable[[], datetime],
) -> dict[str, object]:
    """개인 소스 범위를 유지하고 모든 일치 항목을 찾은 뒤 페이지를 자른다."""
    selected = replace(
        context,
        sources=tuple(
            s
            for s in context.sources
            if s.kind == "task" and (search.section == "all" or s.section == search.section)
        ),
    )

    def matches(task: Task) -> bool:
        """완료 사실은 소스 설정을 따르고 검색은 원본 상태도 보존한다."""
        statuses = context.completed_statuses.get(task.source_id)
        return search.criteria.matches(
            task, completed=statuses is not None and task.status in statuses
        )

    selection = await read_personal_sources(
        None, reader, selected, policy, now, task_filter=matches
    )
    total = 0

    def matching_rows() -> Iterator[dict[str, object]]:
        """페이지 밖의 행도 모두 세고 업무와 QA에 같은 정렬 기준을 적용한다."""
        nonlocal total
        for source_id, group, row in selection.rows(selected):
            if group in {"tasks", "qa"} and source_id not in selected.desktop_areas:
                total += 1
                yield row

    end = search.offset + search.limit
    order = partial(task_sort_key, display_policy=selected.task_display_policy)
    if search.offset >= selection.max_rows:
        # 원본 행 수를 넘어선 페이지도 전체 건수·경고·정렬값 검사는 끝까지 수행한다.
        for row in matching_rows():
            order(row)
        tasks = []
    elif end * 2 >= selection.max_rows:
        # 대부분의 행을 보관해야 하는 뒷페이지는 전체 정렬이 힙보다 저렴하다.
        tasks = sorted(matching_rows(), key=order)[search.offset : end]
    else:
        # 작은 앞페이지는 일치 행을 모두 세면서 필요한 순위까지만 보관한다.
        tasks = nsmallest(end, matching_rows(), key=order)[search.offset :]
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
