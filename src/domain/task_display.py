"""메인 카드의 상태 정렬과 이전 표시 설정의 값을 정의한다."""

from dataclasses import dataclass
from enum import IntEnum


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


@dataclass(frozen=True)
class StatusTaskDisplayPolicy:
    """이전 메인 카드에서 사용한 완료 업무의 기본 보존 일수다."""

    completed_retention_days: int = 7


@dataclass(frozen=True)
class QATaskDisplayPolicy(StatusTaskDisplayPolicy):
    """기존 QA 설정을 메인 카드의 제외 조건으로 이전할 때 읽는다."""

    hidden_completed_rounds: tuple[str, ...] = ()
    hidden_statuses: tuple[str, ...] = ()
