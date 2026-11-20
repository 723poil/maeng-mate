"""외부 API 없이 수집 대기·실패를 주입하는 가상 원천이다."""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from src.domain.models import SourceMapping, WorkItem


@dataclass
class CollectionSource:
    """임시 DB 검증에서 지정한 수집 동작을 원천 계약으로 제공한다."""

    collect_action: Callable[[SourceMapping, str], Awaitable[list[WorkItem]]]

    async def collect(self, source: SourceMapping, observed_at: str) -> list[WorkItem]:
        """검증 시나리오가 주입한 완전 수집 또는 실패를 실행한다."""
        return await self.collect_action(source, observed_at)
