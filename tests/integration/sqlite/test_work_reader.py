"""현재 설정의 정상 스냅샷만 읽고 상태를 변경하지 않는지 검증한다."""

import asyncio
from dataclasses import replace
from uuid import uuid4

import pytest

from src.application.errors import QueryReadError
from src.domain.display_columns import DisplayColumn
from src.domain.models import SourceMapping
from src.infrastructure.notion.normalization import normalize_page
from src.infrastructure.sqlite.connection import Database
from src.infrastructure.sqlite.snapshots import SQLiteSnapshots
from src.infrastructure.sqlite.work_reader import SQLiteWorkReader
from tests.fixtures.notion import OBSERVED, SOURCE, page


def source() -> SourceMapping:
    """시나리오마다 가상 소스를 분리한다."""
    return replace(SOURCE, source_id=f"read-{uuid4()}")


async def publish(db: Database, mapping: SourceMapping) -> None:
    """기존 쓰기 계약으로 정상 스냅샷을 먼저 만든다."""
    store = SQLiteSnapshots(db)
    run = await store.begin_run(mapping, OBSERVED)
    await store.complete_run(run, [normalize_page(page(), mapping, OBSERVED)], OBSERVED)


async def test_reads_only_configured_sources_without_writes(db: Database) -> None:
    """설정에서 제외된 소스를 반환하지 않고 실행·스냅샷을 그대로 둔다."""
    included, excluded = source(), source()
    await publish(db, included)
    await publish(db, excluded)
    before = await db.fetch(
        "SELECT generation,lease_until,last_completed_at FROM work_source_state WHERE source_id=?",
        (included.source_id,),
    )
    result = await SQLiteWorkReader(db).read_sources((included,))
    after = await db.fetch(
        "SELECT generation,lease_until,last_completed_at FROM work_source_state WHERE source_id=?",
        (included.source_id,),
    )
    assert before == after
    assert len(result) == 1
    assert result[0].source_id == included.source_id
    assert result[0].mapping_matches is True
    assert result[0].items[0].id == "fake-page"
    assert result[0].last_success_at is not None


async def test_excludes_unconfirmed_missing_items(db: Database) -> None:
    """현재 목록에서 누락된 항목을 개인 현재 업무로 다시 노출하지 않는다."""
    mapping = source()
    await publish(db, mapping)
    reader = SQLiteWorkReader(db)
    assert (await reader.read_sources((mapping,)))[0].items
    store = SQLiteSnapshots(db)
    run = await store.begin_run(mapping, OBSERVED)
    await store.complete_run(run, [], OBSERVED)
    result = await reader.read_sources((mapping,))
    assert result[0].items == ()
    assert result[0].last_success_at is not None
    assert result[0].latest_run_status == "succeeded"


async def test_changed_mapping_does_not_reuse_old_items(db: Database) -> None:
    """새 필드 매핑에 이전 자료를 정상 결과로 연결하지 않는다."""
    mapping = source()
    await publish(db, mapping)
    reader = SQLiteWorkReader(db)
    assert (await reader.read_sources((mapping,)))[0].items
    changed = replace(mapping, properties={**mapping.properties, "status": "new-status-id"})
    result = await reader.read_sources((changed,))
    assert result[0].mapping_matches is False
    assert result[0].items == ()


async def test_latest_failure_preserves_cached_data(db: Database) -> None:
    """직전 실패와 마지막 정상 수집을 함께 읽는다."""
    mapping = source()
    await publish(db, mapping)
    reader = SQLiteWorkReader(db)
    assert (await reader.read_sources((mapping,)))[0].items
    store = SQLiteSnapshots(db)
    run = await store.begin_run(mapping, OBSERVED)
    await store.fail_run(run, "source_failed")
    result = await reader.read_sources((mapping,))
    assert result[0].latest_run_status == "failed"
    assert len(result[0].items) == 1


async def test_uncollected_source_is_explicit(db: Database) -> None:
    """수집한 적 없는 설정도 빈 정상 자료와 구분해 반환한다."""
    mapping = source()
    result = await SQLiteWorkReader(db).read_sources((mapping,))
    assert len(result) == 1
    assert result[0].last_success_at is None
    assert result[0].mapping_matches is False
    assert result[0].items == ()


async def test_invalid_stored_payload_fails_safely(db: Database) -> None:
    """필수 값이 손상된 저장 자료를 정상 상태로 해석하지 않는다."""
    mapping = source()
    await publish(db, mapping)
    with db.transaction() as connection:
        connection.execute(
            "UPDATE item_snapshots SET payload='{}' WHERE source_id=?",
            (mapping.source_id,),
        )
    with pytest.raises(QueryReadError) as error:
        await SQLiteWorkReader(db).read_sources((mapping,))
    assert mapping.source_id not in str(error.value)


async def test_cached_snapshot_respects_selected_columns(db: Database) -> None:
    """같은 수집 버전에서 동시 조회는 복원본을 공유하고 다른 칼럼 선택에는 재사용하지 않는다."""
    mapping = source()
    item = replace(normalize_page(page(), mapping, OBSERVED), display_values={"t": "가상 제목"})
    store = SQLiteSnapshots(db)
    await store.complete_run(await store.begin_run(mapping, OBSERVED), [item], OBSERVED)
    reader = SQLiteWorkReader(db)
    visible = replace(mapping, display_columns=(DisplayColumn("t", "제목", "title"),))
    first, second = await asyncio.gather(
        reader.read_sources((visible,)), reader.read_sources((visible,))
    )
    assert first[0] is second[0]
    assert first[0].items[0].display_values == {"t": "가상 제목"}
    assert (await reader.read_sources((mapping,)))[0].items[0].display_values == {}
    assert (await reader.read_sources((visible,)))[0] is first[0]


async def test_shares_only_current_sources_across_search_scopes(db: Database) -> None:
    """범위·순서를 바꿔도 같은 복원본을 쓰고 재수집된 소스만 새 자료로 교체한다."""
    one, two = source(), source()
    await publish(db, one)
    await publish(db, two)
    reader = SQLiteWorkReader(db, active_sources=(one, two))
    first, second = await reader.read_sources((one, two))
    subset = await reader.read_sources((two,))
    assert len(subset) == 1 and subset[0] is second
    reverse = await reader.read_sources((two, one))
    assert reverse[0] is second and reverse[1] is first

    store = SQLiteSnapshots(db)
    run = await store.begin_run(one, OBSERVED)
    await store.complete_run(run, [], OBSERVED)
    refreshed, retained = await reader.read_sources((one, two))
    assert not refreshed.items and first.items
    assert retained is second


async def test_search_pages_keep_global_order_and_count_without_completion_settings(
    db: Database,
) -> None:
    """앞·뒤·빈 페이지 모두 원본 행을 집계하고 완료 설정에 의존하지 않는다."""
    from datetime import datetime

    from src.application.task_search import TaskSearch
    from src.application.today_models import TodayContext
    from src.application.use_cases.search_my_tasks import search_my_tasks
    from src.domain.models import Task
    from src.domain.policies import AssignmentPolicy

    tasks, qa = source(), replace(source(), section="qa")
    store = SQLiteSnapshots(db)
    for mapping in (tasks, qa):
        original = normalize_page(page(), mapping, OBSERVED)
        assert isinstance(original, Task)
        items = [
            replace(original, id=f"item-{index}", status="진행 중" if mapping == tasks else "보류")
            for index in range(4)
        ]
        await store.complete_run(await store.begin_run(mapping, OBSERVED), items, OBSERVED)

    context = TodayContext("me", (tasks, qa), {tasks.source_id: ("완료",)})
    reader = SQLiteWorkReader(db, active_sources=context.sources)
    # 마감이 같으면 소스와 항목의 키를 사용해 페이지가 겹치지 않게 한다.
    expected = sorted((mapping.source_id, f"item-{i}") for mapping in (tasks, qa) for i in range(4))
    for offset in (0, 2, 4, 6, 8, 100000):
        result = await search_my_tasks(
            TaskSearch.parse({"limit": 2, "offset": offset}),
            reader,
            context,
            AssignmentPolicy(),
            lambda: datetime.fromisoformat(OBSERVED),
        )
        rows = result["tasks"]
        assert isinstance(rows, list)
        assert [(row["sourceId"], row["id"]) for row in rows] == expected[offset : offset + 2]
        assert result["totalCount"] == 8
        assert result["nextOffset"] == (offset + 2 if offset < 6 else None)
        assert result["warnings"] == []
        assert all("completionState" not in row for row in rows)


async def test_sorted_card_preview_uses_display_columns_from_each_source(db: Database) -> None:
    """두 소스에 같은 항목 ID가 있어도 정렬 뒤 각 소스의 칼럼과 표시 길이를 적용한다."""
    from datetime import datetime

    from src.application.today_models import MainListCard, TodayContext
    from src.application.use_cases.get_my_today import get_my_today
    from src.domain.main_list import ListSort, MainListRules
    from src.domain.models import Task
    from src.domain.policies import AssignmentPolicy

    first, second = source(), source()
    layouts = {
        first.source_id: (DisplayColumn("t", "첫 표", "title"),),
        second.source_id: (DisplayColumn("t", "둘째 표", "title"),),
    }
    sources = tuple(replace(s, display_columns=layouts[s.source_id]) for s in (first, second))
    card = MainListCard("combined", MainListRules(sorts=(ListSort("title"),)), {"title": "text"})
    store = SQLiteSnapshots(db)
    for mapping, title in zip(sources, ("B", "A"), strict=True):
        original = normalize_page(page(), mapping, OBSERVED)
        assert isinstance(original, Task)
        item = replace(original, title=title, display_values={"t": title * 300})
        await store.complete_run(await store.begin_run(mapping, OBSERVED), [item], OBSERVED)
    context = TodayContext(
        "me",
        sources,
        desktop_areas={s.source_id: "default" for s in sources},
        main_cards={s.source_id: card for s in sources},
        display_columns=layouts,
    )
    result = await get_my_today(
        None,
        SQLiteWorkReader(db),
        context,
        AssignmentPolicy(),
        lambda: datetime.fromisoformat(OBSERVED),
    )
    areas = result["categoryAreas"]
    assert isinstance(areas, dict)
    combined = areas["combined"]
    assert combined["count"] == 2
    assert [row["displayFields"] for row in combined["items"]] == [
        [{"label": "둘째 표", "value": "A" * 240}],
        [{"label": "첫 표", "value": "B" * 240}],
    ]
