"""오늘의 출근·휴가·계획 입력과 계산을 조정한다."""

from collections.abc import Callable
from datetime import UTC, datetime

from src.application.date_inputs import parse_day
from src.application.day_ports import DayConflictError, DayInputError, DayStore
from src.domain.daily_work import (
    DayRecord,
    DeparturePolicy,
    parse_clock,
    parse_leave,
    work_instant,
)
from src.domain.work_dates import SEOUL


class DayService:
    """외부 원천 상태와 개인 입력을 섞지 않는 근무 유스케이스다."""

    def __init__(
        self,
        store: DayStore,
        policy: DeparturePolicy,
        *,
        now: Callable[[], datetime],
    ) -> None:
        """규칙을 포함한 기록 저장소와 계산 전략·시계를 주입한다."""
        self._store = store
        self._policy = policy
        self._now = now

    async def read(self) -> dict[str, object]:
        """오늘 기록만 읽고 최초 기록에는 현재 규칙을 표시한다."""
        day = self._now().astimezone(SEOUL).date()
        record = await self._store.read(day)
        return self.view(record)

    async def update(self, payload: object) -> dict[str, object]:
        """모든 입력을 먼저 검증하고 저장 성공값으로만 응답한다."""
        try:
            if not isinstance(payload, dict) or set(payload) != {
                "date",
                "revision",
                "attendanceTime",
                "leaveType",
                "plan",
            }:
                raise ValueError
            raw_day, revision, arrival, plan = (
                payload["date"],
                payload["revision"],
                payload["attendanceTime"],
                payload["plan"],
            )
            if (
                not isinstance(raw_day, str)
                or type(revision) is not int
                or revision < 0
                or not isinstance(plan, str)
                or len(plan) > 4000
            ):
                raise ValueError
            day = parse_day(raw_day)
            leave = parse_leave(payload["leaveType"])
            clock = parse_clock(arrival) if arrival is not None else None
        except ValueError:
            raise DayInputError() from None
        now = self._now()
        if day != now.astimezone(SEOUL).date():
            raise DayConflictError()
        attendance = work_instant(day, clock)
        current = await self._store.read(day)
        # 오늘 메뉴바 자동 저장은 기록 창에서 입력한 실제 퇴근을 지우지 않는다.
        if current.checkout_at is not None and (
            attendance is None or attendance > current.checkout_at
        ):
            raise DayInputError()
        record = DayRecord(
            day,
            revision,
            attendance,
            leave,
            plan,
            current.rules,
            now.astimezone(UTC),
            current.checkout_at,
            current.overtime,
        )
        return self.view(await self._store.save(record, revision))

    def view(self, record: DayRecord) -> dict[str, object]:
        """입력과 계산 근거를 반환하며 미확정 정보를 임의로 보완하지 않는다."""
        result = self._policy.calculate(record)
        local_arrival = record.attendance.astimezone(SEOUL) if record.attendance else None
        return {
            "date": record.day.isoformat(),
            "revision": record.revision,
            "attendanceTime": local_arrival.strftime("%H:%M") if local_arrival else None,
            "leaveType": record.leave.value,
            "plan": record.plan,
            "overtime": record.overtime,
            "actualDepartureAt": record.checkout_at.astimezone(UTC).isoformat()
            if record.checkout_at
            else None,
            "expectedDepartureAt": result.departure.astimezone(UTC).isoformat()
            if result.departure
            else None,
            "calculationState": result.state,
            "requiredMinutes": result.required_minutes,
            "lunchMinutes": result.lunch_minutes,
            "workRulesConfigured": record.rules is not None,
            "previewRules": record.rules.payload() if record.rules else None,
            "warnings": list(record.attendance_warnings),
        }
