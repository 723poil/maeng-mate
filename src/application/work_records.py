"""월별 출퇴근 기록과 명시적 정정·수정 이력을 관리한다."""

from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime
from typing import Protocol

from src.application.date_inputs import parse_day, parse_month
from src.application.day_ports import DayConflictError, DayInputError
from src.application.workday import LEAVE_LABELS, extra_work_label
from src.domain.daily_work import (
    OVERNIGHT_DEPARTURE_REASON,
    DayRecord,
    DeparturePolicy,
    LeaveType,
    departure_due,
    departure_on_record_day,
    parse_clock,
    parse_leave,
    work_instant,
)
from src.domain.work_dates import SEOUL


@dataclass(frozen=True)
class WorkRecordItem:
    """목록·편집에 필요한 실제 시간만 전달하며 예상 퇴근은 포함하지 않는다."""

    day: date
    revision: int
    arrival: str | None
    departure: str | None
    leave: LeaveType
    updated_at: datetime | None
    overtime: bool = False

    def payload(self) -> dict[str, object]:
        """비공개 메모·근무 규칙 없이 명시적인 실제 기록을 직렬화한다."""
        return {
            "date": self.day.isoformat(),
            "revision": self.revision,
            "attendanceTime": self.arrival,
            "departureTime": self.departure,
            "leaveType": self.leave.value,
            "updatedAt": self.updated_at.isoformat() if self.updated_at else None,
            "overtime": self.overtime,
        }


@dataclass(frozen=True)
class SavedRecordHistory:
    """같은 읽기 시점의 확정 기록과 이력 비교용 최신 51개 표시값이다."""

    current: DayRecord
    versions: tuple[WorkRecordItem, ...]


@dataclass(frozen=True)
class RecordRevision:
    """변경 시점과 사용자가 읽을 수 있는 항목별 차이다."""

    revision: int
    updated_at: datetime | None
    changes: tuple[str, ...]


@dataclass(frozen=True)
class WorkRecordDetail:
    """선택 날짜의 실제 기록과 최근 수정 이력이다."""

    item: WorkRecordItem
    history: tuple[RecordRevision, ...]
    truncated: bool = False
    expected_departure: datetime | None = None
    can_confirm: bool = False
    can_edit: bool = True

    @property
    def confirmation_blocked_reason(self) -> str | None:
        """예정 시각을 유지하면서 당일 확정 범위를 벗어난 이유를 제공한다."""
        if self.expected_departure is not None and not departure_on_record_day(
            self.expected_departure, self.item.day
        ):
            return OVERNIGHT_DEPARTURE_REASON
        return None

    def payload(self) -> dict[str, object]:
        """상세 응답에도 원본 계획·회사 규칙을 넣지 않는다."""
        return {
            "record": self.item.payload(),
            "history": [
                {
                    "revision": version.revision,
                    "updatedAt": version.updated_at.isoformat() if version.updated_at else None,
                    "changes": list(version.changes),
                }
                for version in self.history
            ],
            "truncated": self.truncated,
            "expectedDepartureAt": self.expected_departure.astimezone(UTC).isoformat()
            if self.expected_departure
            else None,
            "canConfirm": self.can_confirm,
            "confirmationBlockedReason": self.confirmation_blocked_reason,
            "canEdit": self.can_edit,
        }


@dataclass(frozen=True)
class WorkRecordsMonth:
    """미래를 제외하고 주말도 포함한 월별 기록과 조회 기준 날짜다."""

    month: str
    today: date
    items: tuple[WorkRecordItem, ...]

    def payload(self) -> dict[str, object]:
        """월의 실제 목록을 전달하고 집계는 같은 응답으로 계산하게 한다."""
        return {
            "month": self.month,
            "today": self.today.isoformat(),
            "records": [item.payload() for item in self.items],
        }


@dataclass(frozen=True)
class WorkRecordCorrection:
    """사용자가 선택한 날짜·버전과 실제 출퇴근 입력이다."""

    day: date
    revision: int
    arrival: str | None
    departure: str | None
    leave: LeaveType
    overtime: bool = False

    def payload(self) -> dict[str, object]:
        """메모·회사 규칙은 정정 입력에서 제외해 기존 값을 보존한다."""
        return {
            "date": self.day.isoformat(),
            "revision": self.revision,
            "attendanceTime": self.arrival,
            "departureTime": self.departure,
            "leaveType": self.leave.value,
            "overtime": self.overtime,
        }


class WorkRecordsStore(Protocol):
    """운영 DB 구현과 분리된 월별 읽기·버전별 읽기·명시적 정정 포트다."""

    async def read_month(self, first: date, last: date) -> tuple[WorkRecordItem, ...]:
        """범위 내 확정 기록만 읽고 미기록을 저장하지 않는다."""
        ...

    async def read_history(self, day: date) -> SavedRecordHistory:
        """현재 기록과 이력을 같은 읽기 트랜잭션에서 가져온다."""
        ...

    async def correct(self, record: DayRecord, expected_revision: int) -> DayRecord:
        """주말·과거 기록도 허용하되 미래·버전 충돌을 확인해 원자 저장한다."""
        ...


class WorkRecordsClient(Protocol):
    """창은 구체 DB·HTTP 구현 대신 기록 계약만 사용한다."""

    def month(self, month: str | None = None) -> WorkRecordsMonth:
        """선택한 월의 실제 출퇴근 목록을 조회한다."""
        ...

    def detail(self, day: date) -> WorkRecordDetail:
        """선택 날짜의 확정값과 수정 이력을 조회한다."""
        ...

    def save(self, value: WorkRecordCorrection) -> WorkRecordDetail:
        """읽은 버전이 맞을 때만 정정하며 모호한 실패는 재전송하지 않는다."""
        ...

    def confirm(self, day: date, revision: int) -> WorkRecordDetail:
        """선택한 버전의 퇴근 예정시간을 실제 퇴근으로 명시적으로 확정한다."""
        ...


def record_item(record: DayRecord) -> WorkRecordItem:
    """예정 시각을 사용하지 않고 저장된 출근·퇴근을 서울 시각으로 표시한다."""
    return WorkRecordItem(
        record.day,
        record.revision,
        record.attendance.astimezone(SEOUL).strftime("%H:%M") if record.attendance else None,
        record.checkout_at.astimezone(SEOUL).strftime("%H:%M") if record.checkout_at else None,
        record.leave,
        record.updated_at,
        record.overtime,
    )


def revision_changes(current: WorkRecordItem, previous: WorkRecordItem | None) -> tuple[str, ...]:
    """기록의 실제 시간·휴가 차이만 이력에 표시한다."""
    if previous is None:
        return ("최초 기록",)
    result: list[str] = []
    for label, old, new in (
        ("출근", previous.arrival, current.arrival),
        ("퇴근", previous.departure, current.departure),
        ("휴가", LEAVE_LABELS[previous.leave], LEAVE_LABELS[current.leave]),
        (
            extra_work_label(current.day),
            "있음" if previous.overtime else "없음",
            "있음" if current.overtime else "없음",
        ),
    ):
        if old != new:
            result.append(f"{label} {old or '미기록'} → {new or '미기록'}")
    return tuple(result) or ("기록 저장",)


class WorkRecordsService:
    """과거 정정의 실제 기록과 오늘 예정 계산의 역할을 구분한다."""

    def __init__(
        self,
        store: WorkRecordsStore,
        *,
        now: Callable[[], datetime],
        policy: DeparturePolicy,
    ) -> None:
        """저장소에서 가져온 해당 날짜의 규칙으로 예정 시각을 계산한다."""
        self._store, self._now = store, now
        self._policy = policy

    async def month(self, value: str | None) -> WorkRecordsMonth:
        """미기록·휴무일도 표시하되 조회로 실제 기록을 생성하지 않는다."""
        today = self._now().astimezone(SEOUL).date()
        month = value or today.strftime("%Y-%m")
        first, last = parse_month(month)
        records = {record.day: record for record in await self._store.read_month(first, last)}
        days = (date(first.year, first.month, day) for day in range(last.day, 0, -1))
        return WorkRecordsMonth(
            month,
            today,
            tuple(
                records[day] if day in records else record_item(DayRecord(day))
                for day in days
                if day <= today
            ),
        )

    async def detail(self, day: date) -> WorkRecordDetail:
        """같은 읽기 시점의 기록과 최근50회 이력을 반환한다."""
        self._validate_day(day)
        saved = await self._store.read_history(day)
        versions = saved.versions
        history = tuple(
            RecordRevision(
                record.revision,
                record.updated_at,
                revision_changes(
                    record, versions[index + 1] if index + 1 < len(versions) else None
                ),
            )
            for index, record in enumerate(versions[:50])
        )
        expected = self._policy.calculate(saved.current).departure
        eligible = saved.current.checkout_at is None and departure_due(expected, day, self._now())
        return WorkRecordDetail(
            record_item(saved.current),
            history,
            len(versions) > 50,
            expected,
            eligible,
            True,
        )

    async def confirm(self, day: date, revision: int) -> WorkRecordDetail:
        """클라이언트가 보낸 시각이 아니라 해당 기록의 예정 계산으로 확정한다."""
        self._validate_day(day)
        if type(revision) is not int or revision < 1:
            raise DayInputError()
        current = (await self._store.read_history(day)).current
        if current.revision != revision or current.checkout_at is not None:
            raise DayConflictError()
        expected = self._policy.calculate(current).departure
        if not departure_due(expected, day, self._now()):
            raise DayInputError()
        # 출근·휴가·규칙·메모는 그대로 두고 실제 퇴근과 감사 이력만 확정한다.
        await self._store.correct(replace(current, checkout_at=expected.astimezone(UTC)), revision)
        return await self.detail(day)

    def _validate_day(self, day: date) -> None:
        """주말도 실제 기록을 허용하되 미래 날짜의 근무는 생성하지 않는다."""
        if day > self._now().astimezone(SEOUL).date():
            raise DayInputError()

    async def correct(self, payload: object) -> WorkRecordDetail:
        """실제 시각만 정정하고 기존 메모·근무 규칙과 버전 이력을 보존한다."""
        required = {
            "date",
            "revision",
            "attendanceTime",
            "departureTime",
            "leaveType",
        }
        if (
            not isinstance(payload, dict)
            or not required <= set(payload)
            or set(payload) - required - {"overtime"}
        ):
            raise DayInputError()
        raw_day, revision = payload["date"], payload["revision"]
        if not isinstance(raw_day, str) or type(revision) is not int or revision < 0:
            raise DayInputError()
        try:
            day = parse_day(raw_day)
            arrival = (
                parse_clock(payload["attendanceTime"])
                if payload["attendanceTime"] is not None
                else None
            )
            departure = (
                parse_clock(payload["departureTime"])
                if payload["departureTime"] is not None
                else None
            )
            leave = parse_leave(payload["leaveType"])
        except ValueError:
            raise DayInputError() from None
        self._validate_day(day)
        now = self._now()
        attendance = work_instant(day, arrival)
        checkout = work_instant(day, departure)
        if checkout is not None and (attendance is None or checkout < attendance or checkout > now):
            raise DayInputError()
        current = (await self._store.read_history(day)).current
        if current.revision != revision:
            raise DayConflictError()
        # 구버전 입력에 야근 키가 없으면 기존 체크를 보존한다.
        overtime = payload.get("overtime", current.overtime)
        if type(overtime) is not bool:
            raise DayInputError()
        record = replace(
            current,
            attendance=attendance,
            checkout_at=checkout,
            leave=leave,
            overtime=overtime,
        )
        await self._store.correct(record, revision)
        # 저장 뒤에도 다른 정정이 끼어들 수 있으므로 확정 상세를 다시 읽는다.
        return await self.detail(day)
