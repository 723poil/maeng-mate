"""현재 본인 업무를 제한 전에 검색하고 조회 근거를 함께 반환한다."""

from collections.abc import Callable
from dataclasses import replace
from datetime import datetime

from src.application.personal_work import checked_query_result, read_personal_work
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

    rows = await read_personal_work(
        None, reader, selected, policy, now, task_filter=matches, merge_qa=True
    )
    matches_sorted = rows.groups["tasks"]
    total = len(matches_sorted)
    tasks = matches_sorted[search.offset : search.offset + search.limit]
    next_offset = search.offset + len(tasks)
    return checked_query_result(
        {
            **rows.metadata,
            "truncated": len(tasks) < total,
            "tasks": tasks,
            "totalCount": total,
            "limit": search.limit,
            "offset": search.offset,
            "nextOffset": next_offset if next_offset < total else None,
        }
    )
