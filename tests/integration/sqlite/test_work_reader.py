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
    assert first is second
    assert first[0].items[0].display_values == {"t": "가상 제목"}
    assert (await reader.read_sources((mapping,)))[0].items[0].display_values == {}
    assert (await reader.read_sources((visible,)))[0].items[0].display_values == {"t": "가상 제목"}
