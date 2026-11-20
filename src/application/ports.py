"""유스케이스가 요구하는 원천·저장·조회 계약이다."""

from collections.abc import Awaitable, Callable, Sequence
from typing import Protocol

from src.application.models import CollectedWork, SyncRun
from src.application.task_search import TaskSearch
from src.application.today_models import SourceSnapshot
from src.domain.models import SourceMapping, WorkItem

# HTTP는 구체 저장소 대신 날짜를 받는 조회 함수만 주입받는다.
type PeriodicStatusQuery = Callable[[], Awaitable[dict[str, object]]]
type TodayQuery = Callable[[str | None], Awaitable[dict[str, object]]]


class WorkSource(Protocol):
    """원천 구현을 교체할 수 있는 완전 수집 계약이다."""

    async def collect(
        self, source: SourceMapping, observed_at: str
    ) -> CollectedWork | list[WorkItem]:
        """완전한 결과를 반환하며 원본 스키마를 제공하지 않는 원천은 행만 반환한다."""
        ...


class SnapshotStore(Protocol):
    """원자적 확정과 실패 보존을 제공하는 교체 가능한 저장 계약이다."""

    async def begin_run(self, source: SourceMapping, started_at: str) -> SyncRun:
        """유효한 실행이 없을 때 새 수집 세대를 발급한다."""
        ...

    async def complete_run(
        self,
        run: SyncRun,
        items: Sequence[WorkItem],
        completed_at: str,
        *,
        status_options: tuple[str, ...] | None = None,
    ) -> None:
        """전체 정상 결과를 한 번에 확정한다."""
        ...

    async def fail_run(self, run: SyncRun, code: str, *, retry_after: float | None = None) -> None:
        """다른 실행의 lease를 해제하지 않고 실패를 기록한다."""
        ...


class SnapshotReader(Protocol):
    """스냅샷 읽기 구현을 교체하는 계약이다."""

    async def read_sources(self, sources: Sequence[SourceMapping]) -> tuple[SourceSnapshot, ...]:
        """현재 지정한 소스의 정상 자료와 수집 근거를 읽기만 한다."""
        ...


class McpQueryReader(Protocol):
    """MCP 도구가 사용할 수 있는 네 가지 개인 조회 계약이다."""

    async def read_status(self) -> dict[str, object]:
        """인증된 조회 결과만 반환하고 원문 오류는 공개하지 않는다."""
        ...

    async def read_today(self, requested_date: str | None = None) -> dict[str, object]:
        """날짜만 전달하며 사용자·소스·권한을 입력으로 받지 않는다."""
        ...

    async def read_workday(self) -> dict[str, object]:
        """근무 기록·퇴근 예상과 계산 상태만 읽고 수정하지 않는다."""
        ...

    async def search_tasks(self, search: TaskSearch) -> dict[str, object]:
        """현재 소스·본인 범위에서 검색하며 저장 상태는 바꾸지 않는다."""
        ...
