"""macOS 표현과 무관한 메뉴바 조회·수정 값과 연결 계약이다."""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal, Protocol

from src.application.date_inputs import parse_day
from src.domain.daily_work import (
    DayRecord,
    DeparturePolicy,
    DepartureResult,
    LeaveType,
    WorkRules,
    parse_clock,
    work_instant,
)


@dataclass(frozen=True)
class DayUpdate:
    """사용자가 읽은 날짜와 버전을 수정 요청에 고정한다."""

    day: str
    revision: int
    attendance: str | None
    leave: LeaveType
    plan: str

    def payload(self) -> dict[str, object]:
        """로컬 기록 API에 허용한 키만 전달한다."""
        return {
            "date": self.day,
            "revision": self.revision,
            "attendanceTime": self.attendance,
            "leaveType": self.leave.value,
            "plan": self.plan,
        }


@dataclass(frozen=True)
class DayView:
    """서버가 확정한 근무 기록과 계산 근거다."""

    update: DayUpdate
    departure: datetime | None
    state: str
    required_minutes: int | None
    lunch_minutes: int
    rules_configured: bool
    warnings: tuple[str, ...]
    rules: WorkRules | None = None
    actual_departure: datetime | None = None
    overtime: bool = False


@dataclass(frozen=True)
class MenuEntry:
    """메뉴에 필요한 표시값과 허용된 링크만 보유한다."""

    title: str
    detail: str
    url: str | None
    status: str | None = None
    deadline: str | None = None
    round_name: str | None = None
    source_id: str | None = None
    display_fields: tuple[tuple[str, str], ...] | None = None
    badge_field_indices: frozenset[int] = frozenset()


@dataclass(frozen=True)
class CollectionView:
    """수집 집계와 메뉴바의 표시명 조회에 쓰는 활성 소스 키다."""

    enabled: bool
    running: bool
    succeeded: int
    failed: int
    next_at: datetime | None
    last_success: datetime | None
    interval_seconds: int | None = None
    retrying: int = 0
    retry_attempt: int = 0
    active_source_id: str | None = field(default=None, repr=False)


@dataclass(frozen=True)
class DesktopArea:
    """독립 소스의 미리보기와 잘리기 전 개수다."""

    entries: tuple[MenuEntry, ...]
    count: int


@dataclass(frozen=True)
class DesktopSnapshot:
    """날짜를 맞춰 읽은 근무·수집·Notion 표시값이다."""

    day: DayView
    collection: CollectionView
    tasks: tuple[MenuEntry, ...]
    meetings: tuple[MenuEntry, ...]
    leaves: tuple[MenuEntry, ...]
    counts: tuple[int, int, int]
    stale: bool
    warnings: tuple[str, ...]
    work_error: bool = False
    task_page: str | None = None
    meeting_page: str | None = None
    freshness_reasons: tuple[str, ...] = ()
    qa: tuple[MenuEntry, ...] = ()
    qa_count: int = 0
    qa_page: str | None = None
    page_links_revision: int = 0
    other_events: tuple[MenuEntry, ...] = ()
    source_areas: dict[str, DesktopArea] = field(default_factory=dict)
    category_areas: dict[str, DesktopArea] = field(default_factory=dict)

    def board_page(self, section: Literal["tasks", "qa", "meetings"]) -> str | None:
        """세 영역의 보드 선택을 같은 매핑으로 처리한다."""
        return {"tasks": self.task_page, "qa": self.qa_page, "meetings": self.meeting_page}[section]


class DesktopClient(Protocol):
    """UI는 로컬 HTTP 구현을 알지 않고 조회·수정만 요청한다."""

    def fetch(self) -> DesktopSnapshot:
        """로컬 서버의 확정된 상태를 읽는다."""
        ...

    def set_interval(self, seconds: int) -> DesktopSnapshot:
        """수집 주기를 저장한 뒤 확정 상태를 다시 읽는다."""
        ...

    def save(self, update: DayUpdate) -> DayView:
        """저장 응답만 반환하며 실패 시 무조건 재전송하지 않는다."""
        ...


def preview_departure(
    update: DayUpdate, rules: WorkRules | None, *, policy: DeparturePolicy
) -> DepartureResult:
    """선택값의 예상시간만 계산하며 DB 기록이나 수정 버전을 바꾸지 않는다."""
    day = parse_day(update.day)
    arrival = work_instant(day, parse_clock(update.attendance) if update.attendance else None)
    return policy.calculate(
        DayRecord(day, update.revision, arrival, update.leave, update.plan, rules)
    )
