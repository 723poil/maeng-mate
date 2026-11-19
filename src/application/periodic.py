"""예약된 소스의 수집 순서와 종료 요청을 처리한다."""

import asyncio
from collections.abc import Awaitable, Callable, Sequence
from datetime import UTC, datetime
from typing import Protocol

from src.application.errors import SnapshotError
from src.application.ports import SnapshotStore, WorkSource
from src.application.use_cases.synchronize import synchronize
from src.domain.models import SourceMapping

type IntervalUpdate = Callable[[int], Awaitable[dict[str, object]]]


class ScheduleReader(Protocol):
    """실행 시각과 공개 집계에 필요한 예약 조회 계약이다."""

    async def set_interval(self, sources: Sequence[SourceMapping], seconds: int) -> None:
        """현재 소스의 주기와 사용자 선호값을 원자적으로 저장한다."""
        ...

    async def due_sources_and_delay(
        self, sources: Sequence[SourceMapping]
    ) -> tuple[tuple[SourceMapping, ...], float]:
        """지금 실행할 소스와 다음 예약까지 남은 초를 함께 읽는다."""
        ...

    async def read_status(self, sources: Sequence[SourceMapping]) -> dict[str, object]:
        """식별자와 원문 없는 예약 집계만 반환한다."""
        ...


async def wait_until_stopped(stop: asyncio.Event, delay: float) -> None:
    """종료에 즉시 반응하고 시계 변경·절전 복귀를 주기적으로 재확인한다."""
    try:
        await asyncio.wait_for(stop.wait(), timeout=min(60.0, delay))
    except TimeoutError:
        pass  # DB 예약 시각을 다시 읽으며 놓친 주기를 누적하지 않는다.


def utc_now() -> datetime:
    """기본 시계는 시간대가 명시된 UTC 시점이다."""
    return datetime.now(UTC)


class PeriodicCollector:
    """수집 정책과 원천·예약·저장 구현을 분리하는 실행기다."""

    def __init__(
        self,
        sources: Sequence[SourceMapping],
        reader: WorkSource,
        store: SnapshotStore,
        schedules: ScheduleReader,
        *,
        now: Callable[[], datetime] = utc_now,
        wait: Callable[[asyncio.Event, float], Awaitable[None]] = wait_until_stopped,
    ) -> None:
        """시계와 대기를 주입해 실제 시간 없이 실행을 검증한다."""
        self._sources = tuple(sources)
        self._reader = reader
        self._store = store
        self._schedules = schedules
        self._now = now
        self._wait = wait
        self._changed = asyncio.Event()
        self._enabled = False
        self._running = False

    async def run(self, stop: asyncio.Event) -> None:
        """소스별 실패는 기록하되 저장 실패는 상주 감독자에게 전달한다."""
        self._enabled = True
        try:
            while not stop.is_set():
                # 조회 도중에 바뀐 주기도 대기에 들어가자마자 반영한다.
                self._changed.clear()
                due, delay = await self._schedules.due_sources_and_delay(self._sources)
                if not due:
                    await self._wait_for_change(stop, delay)
                    continue
                for source in due:
                    if stop.is_set():
                        break
                    self._running = True
                    try:
                        result = await synchronize(source, self._reader, self._store, self._now)
                        if result["status"] == "failed" and result["code"] == "storage_failed":
                            raise SnapshotError("storage_failed")
                    finally:
                        self._running = False
        finally:
            self._enabled = False
            self._running = False

    async def set_interval(self, seconds: int) -> dict[str, object]:
        """저장 후 대기를 깨워 새 예약을 읽으며 수집 자체는 재시작하지 않는다."""
        await self._schedules.set_interval(self._sources, seconds)
        self._changed.set()
        return await self.read_status()

    async def _wait_for_change(self, stop: asyncio.Event, delay: float) -> None:
        """종료·예약 시각·화면 주기 변경 중 먼저 도착한 신호를 처리한다."""
        waiting = asyncio.ensure_future(self._wait(stop, delay))
        changed = asyncio.create_task(self._changed.wait())
        try:
            completed, _ = await asyncio.wait(
                (waiting, changed), return_when=asyncio.FIRST_COMPLETED
            )
            if waiting in completed:
                await waiting
        finally:
            waiting.cancel()
            changed.cancel()
            await asyncio.gather(waiting, changed, return_exceptions=True)

    async def read_status(self) -> dict[str, object]:
        """영속 예약 집계에 현재 프로세스의 동작 상태를 덧붙인다."""
        return {
            **await self._schedules.read_status(self._sources),
            "enabled": self._enabled,
            "running": self._running,
        }
