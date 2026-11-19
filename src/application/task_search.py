"""HTTP·MCP에서 공유하는 업무 검색 입력과 조회 계약이다."""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import date
from typing import Literal

from src.application.date_inputs import parse_day
from src.application.errors import InvalidDateError
from src.domain.task_search import TaskCriteria


class TaskSearchInputError(ValueError):
    """검색 원문을 노출하지 않는 입력 오류다."""

    def __init__(self) -> None:
        """허용 형식만 짧게 안내한다."""
        super().__init__("검색어·상태·마감 날짜(YYYY-MM-DD)·대상·개수·offset을 확인하세요.")


def optional_text(value: object) -> str | None:
    """길이를 제한하고 빈 문자열은 조건 없음으로 처리한다."""
    if value is None:
        return None
    if not isinstance(value, str) or len(value) > 100:
        raise TaskSearchInputError()
    return value.strip() or None


def optional_date(value: object) -> date | None:
    """날짜 원문과 달력의 유효성을 확인한다."""
    if value is None:
        return None
    if not isinstance(value, str):
        raise TaskSearchInputError()
    try:
        return parse_day(value)
    except InvalidDateError:
        raise TaskSearchInputError() from None


@dataclass(frozen=True)
class TaskSearch:
    """검증한 검색 조건과 결과 범위다."""

    criteria: TaskCriteria
    section: Literal["tasks", "qa", "all"]
    limit: int
    offset: int

    @classmethod
    def parse(cls, payload: object) -> "TaskSearch":
        """알 수 없는 키·bool 개수·역전 구간을 거부한다."""
        if not isinstance(payload, dict) or set(payload) - {
            "query",
            "status",
            "due_from",
            "due_to",
            "section",
            "limit",
            "offset",
        }:
            raise TaskSearchInputError()
        start, end = optional_date(payload.get("due_from")), optional_date(payload.get("due_to"))
        limit, offset = payload.get("limit", 50), payload.get("offset", 0)
        section = payload.get("section", "all")
        if (
            type(limit) is not int
            or not 1 <= limit <= 100
            or type(offset) is not int
            or not 0 <= offset <= 100000
            or section not in ("tasks", "qa", "all")
            or (start is not None and end is not None and start > end)
        ):
            raise TaskSearchInputError()
        return cls(
            TaskCriteria(
                optional_text(payload.get("query")),
                optional_text(payload.get("status")),
                start,
                end,
            ),
            section,
            limit,
            offset,
        )

    def payload(self) -> dict[str, object]:
        """URL 대신 인증된 검색 본문으로만 조건을 전달한다."""
        return {
            "query": self.criteria.query,
            "status": self.criteria.status,
            "due_from": self.criteria.due_from.isoformat() if self.criteria.due_from else None,
            "due_to": self.criteria.due_to.isoformat() if self.criteria.due_to else None,
            "section": self.section,
            "limit": self.limit,
            "offset": self.offset,
        }


type TaskSearchQuery = Callable[[TaskSearch], Awaitable[dict[str, object]]]
