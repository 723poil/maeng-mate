"""격리된 실제 SQLite에서 저장과 실행 세대를 검증한다."""

import asyncio
import json
from dataclasses import asdict, replace
from datetime import UTC, datetime
from uuid import uuid4

import pytest

from src.application.errors import SnapshotError
from src.application.use_cases.synchronize import synchronize
from src.domain.models import CalendarEvent, Leave, Period, SourceMapping, Task, WorkItem
from src.domain.task_changes import AssignedTaskChangePolicy
from src.infrastructure.notion.normalization import normalize_page
from src.infrastructure.sqlite.connection import Database
from src.infrastructure.sqlite.serialization import fingerprint
from src.infrastructure.sqlite.snapshots import SQLiteSnapshots
from tests.fixtures.notion import OBSERVED, SOURCE, page, task_schema


def source() -> SourceMapping:
    """각 테스트가 다른 소스를 사용하도록 분리한다."""
    return replace(SOURCE, source_id=f"fake-{uuid4()}")


async def test_publish_and_missing(db: Database) -> None:
    """완전 수집만 발행하고 누락 항목의 원문은 보존한다."""
    mapping = source()
    store = SQLiteSnapshots(db)
    first = await store.begin_run(mapping, OBSERVED)
    await store.complete_run(first, [normalize_page(page(), mapping, OBSERVED)], OBSERVED)
    second = await store.begin_run(mapping, OBSERVED)
    await store.complete_run(second, [], OBSERVED)
    rows = await db.fetch(
        "SELECT availability FROM item_snapshots WHERE source_id=?", (mapping.source_id,)
    )
    assert rows == [{"availability": "unconfirmed_missing"}]


async def test_returning_tasks_compare_with_last_seen_values(db: Database) -> None:
    """누락을 반복해도 이전 정상 값과 비교해 재등장한 업무의 실제 변경만 기록한다."""
    mapping = replace(source(), event_alert_triggers=frozenset({"status"}))
    item = normalize_page(page(), mapping, OBSERVED)
    assert isinstance(item, Task)
    store = SQLiteSnapshots(
        db, notification_user_id="me", task_change_policy=AssignedTaskChangePolicy()
    )
    # 한 조회 묶음을 넘는 입력에서 첫 항목과 마지막 항목의 이전 상태를 구분한다.
    tasks = [replace(item, id=f"item-{i}") for i in range(160)]
    for observed in (tasks, [], []):
        await store.complete_run(await store.begin_run(mapping, OBSERVED), observed, OBSERVED)
    changed = replace(tasks[-1], status="검토")
    returned = [changed, *reversed(tasks[:-1])]
    await store.complete_run(await store.begin_run(mapping, OBSERVED), returned, OBSERVED)

    assert await db.fetch("SELECT item_id,kinds FROM task_changes") == [
        {"item_id": changed.id, "kinds": '["status"]'}
    ]
    assert await db.fetch("SELECT state FROM notification_jobs") == [{"state": "pending"}]
    assert await db.fetch("SELECT DISTINCT availability FROM item_snapshots") == [
        {"availability": "present"}
    ]


async def test_rollback(db: Database) -> None:
    """중간 SQL 제약 오류가 정상 스냅샷을 부분 변경하지 않는다."""
    mapping = source()
    store = SQLiteSnapshots(db)
    item = normalize_page(page(), mapping, OBSERVED)
    first = await store.begin_run(mapping, OBSERVED)
    await store.complete_run(first, [item], OBSERVED)
    second = await store.begin_run(mapping, OBSERVED)
    with pytest.raises(SnapshotError):
        await store.complete_run(second, [replace(item, id="new"), replace(item, id="")], OBSERVED)
    rows = await db.fetch(
        "SELECT item_id FROM item_snapshots WHERE source_id=?", (mapping.source_id,)
    )
    assert rows == [{"item_id": "fake-page"}]


async def test_generation_and_parallel_sessions(db: Database) -> None:
    """병렬 실행의 승자를 한 명으로 제한하고 이전 실패가 새 lease를 풀지 못하게 한다."""
    mapping = source()
    store = SQLiteSnapshots(db)
    results = await asyncio.gather(
        store.begin_run(mapping, OBSERVED),
        store.begin_run(mapping, OBSERVED),
        return_exceptions=True,
    )
    winners = [result for result in results if not isinstance(result, BaseException)]
    assert len(winners) == 1
    first = winners[0]
    with db.transaction() as connection:
        connection.execute(
            "UPDATE work_source_state SET lease_until=0 WHERE source_id=?",
            (mapping.source_id,),
        )
    second = await store.begin_run(mapping, OBSERVED)
    await store.fail_run(first, "source_failed")
    with pytest.raises(SnapshotError):
        await store.begin_run(mapping, OBSERVED)
    with pytest.raises(SnapshotError):
        await store.complete_run(first, [], OBSERVED)
    await store.complete_run(second, [], OBSERVED)


async def test_failure_preserves_snapshot(db: Database) -> None:
    """수집 실패를 빈 정상 결과로 확정하지 않는다."""
    mapping = source()
    store = SQLiteSnapshots(db)
    item = normalize_page(page(), mapping, OBSERVED)
    run = await store.begin_run(mapping, OBSERVED)
    await store.complete_run(run, [item], OBSERVED)

    class FailedSource:
        """원문 오류를 업무 결과에 노출하지 않는 외부 경계다."""

        async def collect(self, source: SourceMapping, observed_at: str) -> list[WorkItem]:
            """항상 원천 실패를 반환한다."""
            raise ValueError("private-original-error")

    result = await synchronize(mapping, FailedSource(), store, lambda: datetime.now(UTC))
    assert result == {"status": "failed", "code": "source_failed"}
    rows = await db.fetch(
        "SELECT availability FROM item_snapshots WHERE source_id=?", (mapping.source_id,)
    )
    assert rows == [{"availability": "present"}]


async def test_stored_json_and_hash_remain_compatible(db: Database) -> None:
    """세 항목 유형의 기본값과 선택 칼럼이 이전 저장 바이트와 해시를 유지한다."""
    period = Period("date", "2026-11-19", None)
    identity = ("item", "fixture", "https://notion.so/fixture", OBSERVED, OBSERVED)
    items: tuple[WorkItem, ...] = (
        Task(*identity, "업무", ("p",), None, period, "2차"),
        CalendarEvent(*identity, "일정", ("p",), None, "휴가", "leave", True),
        Leave(*identity, ("p",), period, None, False),
    )
    store = SQLiteSnapshots(db)
    for item in items:
        mapping = replace(SOURCE, source_id=item.source_id, kind=item.kind)
        for row in (
            item,
            replace(
                item,
                display_values={"c": "표시"},
                alert_values={"r": "비교값"},
                alert_due_at={"r": datetime.fromisoformat(OBSERVED)},
                calendar_periods={"c": period},
            ),
        ):
            run = await store.begin_run(mapping, OBSERVED)
            await store.complete_run(run, [row], OBSERVED)
            expected = asdict(row)
            # 예약 계산값은 영속 큐에만 저장하며 이전 스냅샷 JSON과 해시에 넣지 않는다.
            del expected["alert_due_at"]
            if isinstance(row, Task):
                # #29에서 추가된 빈 부모 메타데이터는 이전 저장 형식에 포함되지 않는다.
                assert row.parent_label is None and row.parent_order is None
                del expected["parent_label"]
                del expected["parent_order"]
            for name in ("alert_values", "display_values", "calendar_periods"):
                if not expected[name]:
                    del expected[name]
            facts = {
                k: v for k, v in expected.items() if k not in ("source_edited_at", "observed_at")
            }
            assert await db.fetch(
                "SELECT payload,semantic_hash FROM item_snapshots WHERE source_id=?",
                (item.source_id,),
            ) == [
                {
                    "payload": json.dumps(expected, ensure_ascii=False),
                    "semantic_hash": fingerprint(facts),
                }
            ]


async def test_encoded_alias_reuses_complete_property_and_preserves_snapshot(db: Database) -> None:
    """같은 담당자 칼럼은 끝까지 한 번 읽고 재수집 중 상세 조회가 실패하면 이전 값을 보존한다."""
    import httpx

    from src.domain.display_columns import DisplayColumn
    from src.infrastructure.notion.source import NotionSource
    from src.infrastructure.sqlite.work_reader import SQLiteWorkReader

    mapping = replace(
        source(),
        properties={**SOURCE.properties, "assignees": "p%3F"},
        stored_columns=(DisplayColumn("p?", "담당자", "people"),),
    )
    store = SQLiteSnapshots(db)
    cursors: list[str | None] = []
    fail_last_page = False

    def respond(request: httpx.Request) -> httpx.Response:
        """목록 한 페이지와 두 페이지로 나뉜 가상 사람 속성을 반환한다."""
        if request.method == "GET" and request.url.path.endswith(
            f"/data_sources/{mapping.source_id}"
        ):
            return httpx.Response(200, json=task_schema())
        if request.method == "POST":
            raw = page()
            props = raw["properties"]
            assert isinstance(props, dict)
            props["People"] = {"id": "p%3F", "type": "people", "people": [], "has_more": True}
            return httpx.Response(200, json={"object": "list", "results": [raw], "has_more": False})
        assert request.url.path.endswith("/properties/p?")
        cursor = request.url.params.get("start_cursor")
        cursors.append(cursor)
        if fail_last_page and cursor is not None:
            return httpx.Response(503)
        name = "B" if cursor else "A"
        return httpx.Response(
            200,
            json={
                "object": "list",
                "results": [
                    {"type": "people", "people": {"id": person, "name": person}}
                    for person in (name + "2", name + "1")
                ],
                "has_more": cursor is None,
                "next_cursor": "next" if cursor is None else None,
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        adapter = NotionSource("fixture", client, interval=0)
        result = await synchronize(mapping, adapter, store, lambda: datetime.now(UTC))
        assert result == {"status": "succeeded", "count": 1} and cursors == [None, "next"]
        reader = SQLiteWorkReader(db)
        assert (await reader.read_sources((mapping,)))[0].status_options == ("진행 중", "미사용")
        query = (
            "SELECT payload,semantic_hash,last_seen_run_id FROM item_snapshots WHERE source_id=?"
        )
        previous = await db.fetch(query, (mapping.source_id,))
        payload = previous[0]["payload"]
        assert isinstance(payload, str)
        stored = json.loads(payload)
        # 페이지 내부 순서와 페이지 간 순서 모두 저장된 표시값에 반영된다.
        assert stored["display_values"] == {"p?": "A2, A1, B2, B1"}
        assert stored["assignee_ids"] == ["A1", "A2", "B1", "B2"]

        fail_last_page = True
        result = await synchronize(mapping, adapter, store, lambda: datetime.now(UTC))
        assert result["status"] == "failed" and cursors == [None, "next", None, "next"]
        assert await db.fetch(query, (mapping.source_id,)) == previous
        assert (await reader.read_sources((mapping,)))[0].status_options == ("진행 중", "미사용")
