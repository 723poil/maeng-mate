"""업무 표시 순서와 완료 업무의 노출 기간을 결정한다."""

from dataclasses import dataclass
from datetime import date, datetime
from enum import IntEnum
from typing import Protocol

from src.domain.models import Task
from src.domain.work_dates import SEOUL


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

    def includes(self, task: Task, *, completed: bool, day: date) -> bool:
        """확인한 완료 상태와 기한으로 표시 여부를 판단한다."""
        ...

    def priority(self, status: str | None, *, completed: bool) -> int:
        """동일 그룹 안의 기한 정렬보다 먼저 적용할 순서를 반환한다."""
        ...


@dataclass(frozen=True)
class StatusTaskDisplayPolicy:
    """진행 업무를 먼저 보여주고 오래된 완료 업무만 숨긴다."""

    completed_retention_days: int = 7

    def includes(self, task: Task, *, completed: bool, day: date) -> bool:
        """완료 미확인·기한 없음은 숨기지 않고 정확히7일 지난 기한도 유지한다."""
        if not completed or task.due is None:
            return True
        deadline = task.due.end or task.due.start
        if task.due.kind == "date":
            deadline_day = date.fromisoformat(deadline)
        else:
            instant = datetime.fromisoformat(deadline)
            if instant.tzinfo is None:
                raise ValueError("기한 시점에 시간대가 필요합니다.")
            deadline_day = instant.astimezone(SEOUL).date()
        return (day - deadline_day).days <= self.completed_retention_days

    def priority(self, status: str | None, *, completed: bool) -> int:
        """완료가 확인된 사용자 정의 상태도 완료 그룹에 둔다."""
        return int(TaskDisplayGroup.COMPLETED if completed else display_group(status))


@dataclass(frozen=True)
class QATaskDisplayPolicy(StatusTaskDisplayPolicy):
    """QA 숨김 상태와 차시별 종료 항목의 노출을 결정한다."""

    hidden_completed_rounds: tuple[str, ...] = ()
    hidden_statuses: tuple[str, ...] = ()

    def includes(self, task: Task, *, completed: bool, day: date) -> bool:
        """숨김 상태를 먼저 제외하고 종료 항목은 원천 최종 수정 날짜로 판단한다."""
        if task.status in self.hidden_statuses:
            return False
        if not completed:
            return True
        if task.qa_round in self.hidden_completed_rounds:
            return False
        edited = datetime.fromisoformat(task.source_edited_at)
        if edited.tzinfo is None:
            raise ValueError("QA 수정 시점에 시간대가 필요합니다.")
        return (day - edited.astimezone(SEOUL).date()).days <= self.completed_retention_days
