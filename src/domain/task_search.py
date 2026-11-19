"""상태·제목·서울 날짜 마감 범위의 순수 검색 조건이다."""

from dataclasses import dataclass
from datetime import date, datetime
from unicodedata import normalize

from src.domain.models import Task
from src.domain.task_display import TaskDisplayGroup, display_group
from src.domain.work_dates import SEOUL


@dataclass(frozen=True)
class TaskCriteria:
    """외부 저장소와 무관하게 검색 조건을 함께 적용한다."""

    query: str | None
    status: str | None
    due_from: date | None
    due_to: date | None

    def matches(self, task: Task, *, completed: bool) -> bool:
        """기한 범위는 구간의 마지막 날짜를 사용하며 양 끝 날짜를 포함한다."""
        if (
            self.query
            and normalize("NFC", self.query).casefold()
            not in normalize("NFC", task.title).casefold()
        ):
            return False
        if self.status:
            requested = display_group(self.status)
            actual = TaskDisplayGroup.COMPLETED if completed else display_group(task.status)
            if requested == TaskDisplayGroup.OTHER:
                if self.status.casefold() != (task.status or "").casefold():
                    return False
            elif requested != actual:
                return False
        if self.due_from is None and self.due_to is None:
            return True
        if task.due is None:
            return False
        deadline = task.due.end or task.due.start
        day = (
            date.fromisoformat(deadline)
            if task.due.kind == "date"
            else datetime.fromisoformat(deadline).astimezone(SEOUL).date()
        )
        return (self.due_from is None or day >= self.due_from) and (
            self.due_to is None or day <= self.due_to
        )
