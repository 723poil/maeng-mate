"""본인의 업무·일정과 소스별 화면 미리보기를 반환한다."""

from collections.abc import Callable
from datetime import datetime

from src.application.personal_work import checked_query_result, read_personal_work
from src.application.ports import SnapshotReader
from src.application.today_models import TodayContext
from src.domain.policies import RelevancePolicy


async def get_my_today(
    requested_date: str | None,
    reader: SnapshotReader,
    context: TodayContext,
    policy: RelevancePolicy,
    now: Callable[[], datetime],
) -> dict[str, object]:
    """오늘 조회에는 목록별 미리보기 한도와 최신 보드 주소를 적용한다."""
    rows = await read_personal_work(requested_date, reader, context, policy, now)
    result: dict[str, object] = {
        **rows.metadata,
        "sourcePagesRevision": context.page_links_revision,
        "sourcePages": {
            name: list(
                dict.fromkeys(
                    context.source_pages[source.source_id]
                    for source in context.sources
                    if source.kind == kind
                    and source.source_id in context.source_pages
                    and (kind != "task" or source.section == name)
                )
            )
            for name, kind in (("tasks", "task"), ("qa", "task"), ("meetings", "meeting"))
        },
        **{name: values[:500] for name, values in rows.groups.items()},
        "totalCounts": {name: len(values) for name, values in rows.groups.items()},
        "truncated": any(len(values) > 500 for values in rows.groups.values()),
    }
    if rows.category_areas:
        result["categoryAreas"] = rows.category_areas
    if rows.desktop_areas:
        result["desktopAreas"] = rows.desktop_areas
    return checked_query_result(result)
