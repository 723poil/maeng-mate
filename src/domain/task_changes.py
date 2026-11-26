"""본인 업무의 실제 변경만 고르는 순수 비교 전략이다."""

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol

from src.domain.models import Period, Task
from src.domain.work_dates import canonical_identity, seoul_datetime


@dataclass(frozen=True)
class TaskFacts:
    """알림 판단에 필요한 담당 여부·상태·기한만 보존한다."""

    assigned: bool
    status: str | None
    due: Period | None


def canonical_due(period: Period | None) -> Period | None:
    """같은 시점의 timezone 표기와 같은 끝값 생략을 의미가 같은 값으로 맞춘다."""
    if period is None:
        return None
    if period.kind == "date":
        return Period("date", period.start, period.end or period.start)

    def utc(value: str) -> str:
        """시간대 없는 기한은 서버 시간으로 추측하지 않는다."""
        instant = datetime.fromisoformat(value)
        if instant.tzinfo is None:
            raise ValueError("기한 시점에 시간대가 필요합니다.")
        return instant.astimezone(UTC).isoformat()

    return Period("instant", utc(period.start), utc(period.end or period.start))


def task_facts(task: Task, user_id: str) -> TaskFacts:
    """담당자 목록 순서와 UUID 표기 차이를 본인 배정 변경으로 취급하지 않는다."""
    user = canonical_identity(user_id)
    assigned = any(canonical_identity(person) == user for person in task.assignee_ids)
    return TaskFacts(assigned, task.status, canonical_due(task.due))


def due_description(period: Period | None) -> str:
    """기한의 범위와 실제 정밀도를 유지해 바뀐 시각을 구별한다."""
    if period is None:
        return "없음"

    def label(value: str) -> str:
        """보통은 분까지만 쓰고 초나 소수초가 있으면 그 값까지 표시한다."""
        if period.kind == "date":
            return value.replace("-", ".")
        local = seoul_datetime(datetime.fromisoformat(value))
        if local is None:
            return value
        if local.microsecond:
            return local.strftime("%Y.%m.%d %H:%M:%S.%f").rstrip("0")
        return local.strftime("%Y.%m.%d %H:%M:%S" if local.second else "%Y.%m.%d %H:%M")

    first = label(period.start)
    last = label(period.end) if period.end else first
    return first if first == last else first + "~" + last


@dataclass(frozen=True)
class TaskChange:
    """한 업무에서 함께 바뀐 사실을 하나의 이벤트로 묶는다."""

    kinds: tuple[str, ...]
    before: TaskFacts | None
    after: TaskFacts

    def description(self) -> str:
        """원문을 과도하게 붙이지 않고 변경 이유를 알린다."""
        if "assigned" in self.kinds:
            return "이 업무를 맡게 됐습니다."
        if "unassigned" in self.kinds:
            return "내 담당 업무에서 빠졌습니다."
        parts: list[str] = []
        if self.before is not None:
            if "status" in self.kinds:
                parts.append(
                    f"상태 {self.before.status or '미지정'} → {self.after.status or '미지정'}"
                )
            if "due" in self.kinds:
                parts.append(
                    f"기한 {due_description(self.before.due)} → {due_description(self.after.due)}"
                )
        return " · ".join(parts)


class TaskChangePolicy(Protocol):
    """회사별 변경 판단을 SDK·저장 구현과 독립적으로 교체한다."""

    def compare(self, previous: Task | None, current: Task, user_id: str) -> TaskChange | None:
        """변경할 내용이 없거나 본인과 무관하면 이벤트를 만들지 않는다."""
        ...


class AssignedTaskChangePolicy:
    """현재와 이전 본인 배정 여부를 기준으로 업무 변화를 고른다."""

    def compare(self, previous: Task | None, current: Task, user_id: str) -> TaskChange | None:
        """새 배정은 한 번만, 계속 담당인 업무는 상태·기한만 비교한다."""
        before = task_facts(previous, user_id) if previous else None
        after = task_facts(current, user_id)
        if after.assigned and (before is None or not before.assigned):
            return TaskChange(("assigned",), before, after)
        if before is None or not before.assigned:
            return None
        if not after.assigned:
            return TaskChange(("unassigned",), before, after)
        kinds = tuple(
            name
            for name, changed in (
                ("status", before.status != after.status),
                ("due", before.due != after.due),
            )
            if changed
        )
        return TaskChange(kinds, before, after) if kinds else None
