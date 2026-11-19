"""회사별 업무 관련성 판단을 교체하는 순수 전략이다."""

from collections.abc import Sequence
from typing import Protocol


class RelevancePolicy(Protocol):
    """회사별 업무 관련성 판단을 교체한다."""

    def includes(self, assignees: Sequence[str], participants: Sequence[str], user_id: str) -> bool:
        """명시적인 사용자 관계로 관련성을 판단한다."""
        ...


class AssignmentPolicy:
    """본인 담당 항목만 포함한다."""

    def includes(self, assignees: Sequence[str], participants: Sequence[str], user_id: str) -> bool:
        """단순 참여 여부는 이 전략에 포함하지 않는다."""
        return user_id in assignees


class ParticipationPolicy:
    """본인 담당 또는 참여 항목을 포함한다."""

    def includes(self, assignees: Sequence[str], participants: Sequence[str], user_id: str) -> bool:
        """담당·참여 관계 중 하나가 확인되면 포함한다."""
        return user_id in assignees or user_id in participants
