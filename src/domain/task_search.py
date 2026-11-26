"""상태·제목·서울 날짜 마감 범위의 순수 검색 조건이다."""

from dataclasses import dataclass
from datetime import date
from unicodedata import normalize

from src.domain.models import Task
from src.domain.work_dates import deadline_basis


@dataclass(frozen=True)
class TaskCriteria:
    """외부 저장소와 무관하게 검색 조건을 함께 적용한다."""

    query: str | None
    statuses: frozenset[str] | None
    due_from: date | None
    due_to: date | None
    include_unset: bool = False

    def matches(self, task: Task) -> bool:
        """기한 범위는 구간의 마지막 날짜를 사용하며 양 끝 날짜를 포함한다."""
        if (
            self.query
            and normalize("NFC", self.query).casefold()
            not in normalize("NFC", task.title).casefold()
        ):
            return False
        # 상태 이름은 표마다 의미가 달라 공백·대소문자와 언어를 그대로 비교한다.
        if self.statuses is not None or self.include_unset:
            if task.status is None:
                if not self.include_unset:
                    return False
            elif self.statuses is None or task.status not in self.statuses:
                return False
        if self.due_from is None and self.due_to is None:
            return True
        if task.due is None:
            return False
        day, _ = deadline_basis(task.due)
        return (self.due_from is None or day >= self.due_from) and (
            self.due_to is None or day <= self.due_to
        )
