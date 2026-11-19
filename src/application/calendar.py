"""본인에게 연결된 현재 스냅샷과 개인 근무 기록을 월 달력에 투영한다."""

import asyncio
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime, timedelta
from typing import Protocol

from src.application.date_inputs import parse_month
from src.application.errors import QueryReadError
from src.application.notion_links import notion_link
from src.application.today_metadata import source_metadata
from src.application.today_models import SourceSnapshot
from src.application.work_records import WorkRecordItem
from src.domain.calendar import (
    CALENDAR_GRID_DAYS,
    CalendarAttendance,
    CalendarItem,
    CalendarKind,
    CalendarMonth,
)
from src.domain.display_columns import labelled_values
from src.domain.models import CalendarEvent, Leave, Period, SourceMapping, Task, WorkItem
from src.domain.policies import RelevancePolicy
from src.domain.work_dates import SEOUL, canonical_identity


@dataclass(frozen=True)
class CalendarSource:
    """월 조회에 사용할 매핑과 달력 전용 표시 선택이다."""

    mapping: SourceMapping = field(repr=False)
    visible: bool
    color: str | None
    category_key: str | None


class CalendarSnapshotReader(Protocol):
    """달력의 고정 소스에 대해 마지막 읽기와 동일한지 확인한다."""

    async def read_if_changed(
        self, sources: Sequence[SourceMapping], previous: str | None
    ) -> tuple[str, tuple[SourceSnapshot, ...] | None]:
        """버전이 같으면 None, 바뀌면 같은 읽기 시점의 전체 스냅샷을 반환한다."""
        ...


@dataclass(frozen=True)
class _CalendarProjection:
    """마지막 정상 수집에서 달력에 필요한 일정과 신선도 근거만 보관한다."""

    revision: str
    items: tuple[CalendarItem, ...]
    metadata: tuple[SourceSnapshot, ...]


class CalendarRecordsReader(Protocol):
    """달력에는 개인 기록의 읽기 권한만 제공한다."""

    async def read_month(self, first: date, last: date) -> tuple[WorkRecordItem, ...]:
        """격자에 포함된 날짜 범위의 실제 기록만 읽는다."""
        ...


class CalendarClient(Protocol):
    """두 달력 화면은 조회 구현 대신 월별 읽기 계약을 사용한다."""

    def month(self, month: str | None = None) -> CalendarMonth:
        """현재 월 또는 지정한 월의 저장 자료만 읽는다."""
        ...


def calendar_span(month: str) -> tuple[date, date]:
    """일요일 시작 6주를 고정해 월 이동 시 격자가 흔들리지 않게 한다."""
    first, _ = parse_month(month)
    start = first - timedelta(days=(first.weekday() + 1) % 7)
    return start, start + timedelta(days=CALENDAR_GRID_DAYS - 1)


def period_span(period: Period) -> tuple[date, date, datetime | None, datetime | None]:
    """날짜 끝은 포함하고 시점 끝 자정은 다음 날 일정으로 중복하지 않는다."""
    try:
        if period.kind == "date":
            first = date.fromisoformat(period.start)
            last = date.fromisoformat(period.end) if period.end else first
            if last < first:
                raise QueryReadError()
            return first, last, None, None
        start = datetime.fromisoformat(period.start)
        end = datetime.fromisoformat(period.end) if period.end else None
        if start.tzinfo is None or (end is not None and (end.tzinfo is None or end < start)):
            raise QueryReadError()
        local = start.astimezone(SEOUL)
        final = (
            (end - timedelta(microseconds=1)).astimezone(SEOUL) if end and end > start else local
        )
        return (
            local.date(),
            final.date(),
            start.astimezone(UTC),
            end.astimezone(UTC) if end else None,
        )
    except (ValueError, OverflowError):
        raise QueryReadError() from None


def project_item(
    item: WorkItem,
    source: CalendarSource,
    user: str,
    policy: RelevancePolicy,
) -> CalendarItem | None:
    """개인 범위와 취소 여부만 적용하고 메인 목록의 상태 필터는 사용하지 않는다."""
    mapping = source.mapping
    kind: CalendarKind
    status: str | None = None
    start_date: date | None = None
    due_date: date | None = None
    selected = mapping.calendar_column
    chosen = item.calendar_periods.get(selected.value_key) if selected is not None else None
    if mapping.calendar_date_override and chosen is None:
        return None
    if isinstance(item, Task):
        due = chosen or item.due
        if due is None or (
            mapping.has_person_scope
            and not policy.includes(tuple(map(canonical_identity, item.assignee_ids)), (), user)
        ):
            return None
        # 달력은 날짜로 탐색하므로 완료·보류·과거 차시도 해당 기간에 표시한다.
        first, last, start, end = period_span(due)
        # 업무 기간은 시작부터 기한까지 표시한다. 단일 기한을 시작일로 추정하지 않는다.
        if due.end is not None and not mapping.calendar_date_override:
            start_date = first
            if end is not None:
                last = end.astimezone(SEOUL).date()
        due_date = None if mapping.calendar_date_override else last
        kind, title, status = (
            "other"
            if mapping.calendar_date_override
            else mapping.section
            if mapping.section == "qa"
            else "task",
            item.title,
            item.status,
        )
        all_day = due.kind == "date"
    elif isinstance(item, CalendarEvent):
        period = chosen or item.period
        if (
            item.cancelled
            or period is None
            or (
                mapping.has_person_scope
                and user not in map(canonical_identity, item.participant_ids)
            )
        ):
            return None
        first, last, start, end = period_span(period)
        kind, title, all_day = item.event_type, item.title, period.kind == "date"
        if kind == "leave":
            status = "승인 미판정"
    elif isinstance(item, Leave):
        if user not in map(canonical_identity, item.person_ids):
            return None
        period = chosen or item.period
        first, last, start, end = period_span(period)
        kind, title, all_day = "leave", "휴가", period.kind == "date"
        status = "승인" if item.approved else "승인 확인 필요"
    else:
        return None
    return CalendarItem(
        f"{kind}:{item.source_id}:{item.id}",
        kind,
        title,
        first,
        last,
        start,
        end,
        all_day,
        notion_link(item.url),
        status,
        start_date,
        due_date,
        source.color,
        source.category_key,
        labelled_values(mapping.display_columns, item.display_values)
        if mapping.display_columns is not None
        else None,
    )


class CalendarService:
    """스냅샷·근무를 각각 한 번 읽고 조회만으로 월 달력을 구성한다."""

    def __init__(
        self,
        reader: CalendarSnapshotReader,
        records: CalendarRecordsReader,
        sources: tuple[CalendarSource, ...],
        policy: RelevancePolicy,
        *,
        user_id: str,
        max_age_seconds: int,
        now: Callable[[], datetime],
    ) -> None:
        """bootstrap이 검증한 개인 범위와 읽기 포트만 주입한다."""
        self._reader = reader
        self._records = records
        self._policy = policy
        self._user = canonical_identity(user_id)
        self._max_age_seconds = max_age_seconds
        self._now = now
        # 모든 소스를 숨긴 달력도 설정된 상태이며, 숨긴 소스의 행은 읽지 않는다.
        self._configured = bool(sources)
        self._visible_sources = tuple(source for source in sources if source.visible)

        self._projection: _CalendarProjection | None = None
        self._projection_lock = asyncio.Lock()

    async def _read_projection(self) -> _CalendarProjection:
        """동시 월 조회도 한 번만 변환하고 실패한 결과는 기존 자료에 덮어쓰지 않는다."""
        async with self._projection_lock:
            cached = self._projection
            revision, snapshots = await self._reader.read_if_changed(
                tuple(source.mapping for source in self._visible_sources),
                cached.revision if cached else None,
            )
            if snapshots is None:
                if cached is None or cached.revision != revision:
                    raise QueryReadError()
                return cached
            projection = await asyncio.to_thread(self._project_sources, revision, snapshots)
            self._projection = projection
            return projection

    def _project_sources(
        self, revision: str, snapshots: tuple[SourceSnapshot, ...]
    ) -> _CalendarProjection:
        """월과 무관한 본인 범위·일정 구간·정렬을 수집 버전당 한 번 계산한다."""
        metadata: list[SourceSnapshot] = []
        projected: list[CalendarItem] = []
        by_id = {snapshot.source_id: snapshot for snapshot in snapshots}
        if len(by_id) != len(snapshots):
            raise QueryReadError()
        for source in self._visible_sources:
            snapshot = by_id.get(source.mapping.source_id)
            if snapshot is None:
                continue
            if snapshot.kind != source.mapping.kind:
                raise QueryReadError()
            metadata.append(replace(snapshot, items=()))
            if not snapshot.mapping_matches:
                continue
            for raw in snapshot.items:
                item = project_item(raw, source, self._user, self._policy)
                if item is not None:
                    projected.append(item)
        projected.sort(
            key=lambda item: (
                item.first,
                not item.all_day,
                item.start_at.isoformat() if item.start_at else "",
                item.kind,
                item.title,
                item.key,
            )
        )
        return _CalendarProjection(revision, tuple(projected), tuple(metadata))

    async def month(self, month: str | None) -> CalendarMonth:
        """설정 변경·수집·알림·근무 정정 없이 현재 저장 자료를 읽는다."""
        initial = self._now()
        if initial.tzinfo is None:
            raise QueryReadError()
        key = month or initial.astimezone(SEOUL).strftime("%Y-%m")
        first, last = calendar_span(key)
        projection, records = await asyncio.gather(
            self._read_projection(), self._records.read_month(first, last)
        )
        now = self._now()
        if now.tzinfo is None:
            raise QueryReadError()
        stale = (
            not self._configured
            or len(projection.metadata) != len(self._visible_sources)
            or any(
                source_metadata(snapshot, now, self._max_age_seconds)["stale"]
                for snapshot in projection.metadata
            )
        )
        # 종류별 상한으로 한 종류의 대량 자료가 다른 종류의 일정을 밀어내지 않게 한다.
        limited: list[CalendarItem] = []
        counts: dict[CalendarKind, int] = {}
        truncated = False
        for item in projection.items:
            if item.first > last:
                break  # 시작 날짜순이므로 이 뒤의 일정도 격자 밖이다.
            if item.last < first:
                continue
            counts[item.kind] = counts.get(item.kind, 0) + 1
            if counts[item.kind] <= 300:
                limited.append(item)
            else:
                truncated = True
        attendance = tuple(
            CalendarAttendance(
                record.day,
                record.arrival,
                record.departure,
                record.leave,
                record.overtime,
            )
            for record in records
        )
        return CalendarMonth(
            key,
            now.astimezone(SEOUL).date(),
            first,
            last,
            tuple(limited),
            attendance,
            stale,
            truncated,
            self._configured,
            now.astimezone(UTC),
        )
