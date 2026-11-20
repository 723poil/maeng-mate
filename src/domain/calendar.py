"""달력의 일정·출퇴근 표시와 종류별 선택 값이다."""

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Literal

from src.domain.daily_work import LeaveType

CALENDAR_GRID_DAYS = 6 * 7

CalendarKind = Literal["meeting", "leave", "other", "task", "qa", "attendance"]
CALENDAR_KINDS: tuple[CalendarKind, ...] = ("meeting", "leave", "other", "task", "qa", "attendance")


@dataclass(frozen=True)
class CalendarPreferences:
    """메인과 별도 창에서 함께 사용하는 표시 선택이다."""

    visible_kinds: tuple[CalendarKind, ...] = CALENDAR_KINDS
    hidden_categories: tuple[str, ...] = ()
    shown_categories: tuple[str, ...] = ()

    def is_visible(self, key: str) -> bool:
        """출근 기록과 사용자 카테고리의 표시 선택을 구분한다."""
        return (
            "attendance" in self.visible_kinds
            if key == "attendance"
            else key not in self.hidden_categories
        )

    def shows_item(self, kind: CalendarKind, category: str | None) -> bool:
        """명시적으로 표시한 카테고리를 우선하고 그 밖에는 종류와 숨김 설정을 적용한다."""
        return category in self.shown_categories or (
            category not in self.hidden_categories and kind in self.visible_kinds
        )


@dataclass(frozen=True)
class CalendarItem:
    """날짜 구간과 실제 시점을 구분한 읽기 전용 달력 일정이다."""

    key: str = field(repr=False)
    kind: CalendarKind
    title: str = field(repr=False)
    first: date
    last: date
    start_at: datetime | None
    end_at: datetime | None
    all_day: bool
    url: str | None = field(repr=False)
    status: str | None = None
    start_date: date | None = None
    due_date: date | None = None
    color: str | None = None
    category_key: str | None = None
    display_fields: tuple[tuple[str, str], ...] | None = None
    category: str | None = None
    badge_field_indices: frozenset[int] = frozenset()

    def occurs_on(self, day: date) -> bool:
        """자정 종료를 반영해 미리 정규화한 날짜 구간으로 판정한다."""
        return self.first <= day <= self.last


@dataclass(frozen=True)
class CalendarAttendance:
    """텍스트 일정과 분리해 날짜 옆 아이콘으로 표시할 개인 근무 기록이다."""

    day: date
    arrival: str | None
    departure: str | None
    leave: LeaveType
    overtime: bool


@dataclass(frozen=True)
class CalendarDay:
    """격자와 상세에서 같은 순서로 표시할 일정과 근무 기록이다."""

    items: tuple[CalendarItem, ...] = ()
    attendance: CalendarAttendance | None = None


@dataclass(frozen=True)
class CalendarMonth:
    """월 격자와 선택 날짜 상세가 공유하는 현재 저장 자료다."""

    month: str
    today: date
    first: date
    last: date
    items: tuple[CalendarItem, ...]
    attendance: tuple[CalendarAttendance, ...]
    stale: bool
    truncated: bool
    source_configured: bool
    observed_at: datetime

    def visible_days(self, preferences: CalendarPreferences) -> dict[date, CalendarDay]:
        """각 일정을 표시할 격자 날짜에만 배치하고 월 응답의 정렬 순서를 보존한다."""
        first, last = self.first.toordinal(), self.last.toordinal()
        items: dict[date, list[CalendarItem]] = {
            date.fromordinal(ordinal): [] for ordinal in range(first, last + 1)
        }
        for item in self.items:
            if not preferences.shows_item(item.kind, item.category_key):
                continue
            # 여러 달에 걸친 일정도 현재 격자와 겹치는 날짜만 펼친다.
            start = max(first, item.first.toordinal())
            end = min(last, item.last.toordinal())
            for ordinal in range(start, end + 1):
                items[date.fromordinal(ordinal)].append(item)
        attendance = (
            {item.day: item for item in self.attendance}
            if preferences.is_visible("attendance")
            else {}
        )
        return {
            day: CalendarDay(tuple(entries), attendance.get(day)) for day, entries in items.items()
        }

    def items_on(
        self,
        day: date,
        visible: tuple[CalendarKind, ...],
        hidden_categories: tuple[str, ...] = (),
        shown_categories: tuple[str, ...] = (),
    ) -> tuple[CalendarItem, ...]:
        """필터 변경은 재조회 없이 같은 월 자료에서 즉시 적용한다."""
        preferences = CalendarPreferences(visible, hidden_categories, shown_categories)
        return tuple(
            item
            for item in self.items
            if item.occurs_on(day) and preferences.shows_item(item.kind, item.category_key)
        )

    def attendance_on(self, day: date) -> CalendarAttendance | None:
        """실제로 저장된 기록만 반환하고 미기록을 출근으로 추정하지 않는다."""
        return next((item for item in self.attendance if item.day == day), None)
