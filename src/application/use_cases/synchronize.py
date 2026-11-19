"""완전 수집과 원자적 확정 순서를 통제한다."""

import asyncio
from collections.abc import Callable
from datetime import datetime
from typing import Literal, NotRequired, TypedDict

from src.application.errors import SnapshotError, SourceReadError
from src.application.ports import SnapshotStore, WorkSource
from src.domain.models import SourceMapping


class SyncSucceeded(TypedDict):
    """확정한 항목 수를 돌려준다."""

    status: Literal["succeeded"]
    count: int


class SyncSkipped(TypedDict):
    """실행권을 얻지 않은 이유를 돌려준다."""

    status: Literal["skipped"]
    reason: str


class SyncFailed(TypedDict):
    """실패한 단계와 공개 가능한 원천 오류 분류를 돌려준다."""

    status: Literal["failed"]
    code: Literal["source_failed", "stale_run", "storage_failed"]
    reason: NotRequired[str]


async def synchronize(
    source: SourceMapping, reader: WorkSource, store: SnapshotStore, now: Callable[[], datetime]
) -> SyncSucceeded | SyncSkipped | SyncFailed:
    """외부 조회를 DB 트랜잭션 밖에서 실행하고 부분 결과는 확정하지 않는다."""
    started = now().isoformat()
    begin = asyncio.create_task(store.begin_run(source, started))
    try:
        # 스레드의 실행권 획득은 취소만으로 중단되지 않으므로 결과를 회수한다.
        run = await asyncio.shield(begin)
    except asyncio.CancelledError:
        try:
            acquired = await begin
        except SnapshotError:
            pass
        else:
            await store.fail_run(acquired, "cancelled")
        raise
    except SnapshotError as error:
        if error.code in {"source_busy", "source_not_due"}:
            return {"status": "skipped", "reason": error.code}
        raise
    try:
        items = await reader.collect(source, started)
    except asyncio.CancelledError:
        await store.fail_run(run, "cancelled")
        raise
    except SourceReadError as error:
        await store.fail_run(run, error.code, retry_after=error.retry_after)
        return {"status": "failed", "code": "source_failed", "reason": error.code}
    except Exception:
        await store.fail_run(run, "source_failed")
        return {"status": "failed", "code": "source_failed"}
    complete = asyncio.create_task(store.complete_run(run, items, now().isoformat()))
    try:
        await asyncio.shield(complete)
        return {"status": "succeeded", "count": len(items)}
    except asyncio.CancelledError:
        try:
            await complete
        except SnapshotError:
            await store.fail_run(run, "storage_failed")
        raise
    except SnapshotError as error:
        # 시계 이동·다른 세대의 실행권 획득은 DB 장애가 아니므로 다음 주기에 복구한다.
        if error.code == "stale_run":
            await store.fail_run(run, "source_failed")
            return {"status": "failed", "code": "stale_run"}
        await store.fail_run(run, "storage_failed")
        return {"status": "failed", "code": "storage_failed"}
