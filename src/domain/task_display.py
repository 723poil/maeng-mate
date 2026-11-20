"""메인 카드의 상태 정렬과 이전 표시 설정의 값을 정의한다."""

from dataclasses import dataclass
from enum import IntEnum
from typing import Protocol


class TaskDisplayGroup(IntEnum):
    """표시 순서만 정의하며 실제 완료 판정은 소스 설정을 따른다."""

    IN_PROGRESS = 0
    NOT_STARTED = 1
    REVIEW = 2
    HOLD = 3
    COMPLETED = 4
    OTHER = 5


def display_group(status: str | None) -> TaskDisplayGroup:
    """한글·영문 표시명을 정규화하고 알려진 상태만 묶는다."""
    value = "".join((status or "").casefold().split()).replace("_", "").replace("-", "")
    if value in {"진행", "진행중", "작업중", "수정중", "inprogress", "doing"}:
        return TaskDisplayGroup.IN_PROGRESS
    if value in {
        "시작전",
        "미시작",
        "대기",
        "접수",
        "할일",
        "notstarted",
        "todo",
        "backlog",
        "ready",
    }:
        return TaskDisplayGroup.NOT_STARTED
    if value in {"리뷰", "리뷰중", "검토", "검토중", "확인요청", "review", "inreview"}:
        return TaskDisplayGroup.REVIEW
    if value in {"홀드", "보류", "hold", "onhold"}:
        return TaskDisplayGroup.HOLD
    if value in {"완료", "done", "complete", "completed"}:
        return TaskDisplayGroup.COMPLETED
    return TaskDisplayGroup.OTHER


class TaskDisplayPolicy(Protocol):
    """업무 표시·정렬 규칙을 교체할 수 있는 순수 계약이다."""

    def priority(self, status: str | None, *, completed: bool) -> int:
        """동일 그룹 안의 기한 정렬보다 먼저 적용할 순서를 반환한다."""
        ...


@dataclass(frozen=True)
class StatusTaskDisplayPolicy:
    """카드의 상태 정렬과 예전 완료 업무의 보존 일수를 제공한다."""

    completed_retention_days: int = 7

    def priority(self, status: str | None, *, completed: bool) -> int:
        """완료가 확인된 사용자 정의 상태도 완료 그룹에 둔다."""
        return int(TaskDisplayGroup.COMPLETED if completed else display_group(status))


@dataclass(frozen=True)
class QATaskDisplayPolicy(StatusTaskDisplayPolicy):
    """기존 QA 설정을 메인 카드의 제외 조건으로 이전할 때 읽는다."""

    hidden_completed_rounds: tuple[str, ...] = ()
    hidden_statuses: tuple[str, ...] = ()
