"""개인 근무 입력과 계산 근거를 SQLite에 버전별로 보존한다."""

import asyncio
import json
import sqlite3
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, date, datetime

from src.application.day_ports import DayConflictError
from src.application.errors import SnapshotError
from src.application.work_records import SavedRecordHistory, WorkRecordItem, record_item
from src.domain.daily_work import DayRecord, DeparturePolicy, WorkRules, parse_leave, parse_rules
from src.domain.work_dates import SEOUL
from src.infrastructure.sqlite.connection import Database
from src.infrastructure.sqlite.notifications import enqueue_departure


def utc_now() -> datetime:
    """저장 경계의 기본 시계는 UTC를 사용한다."""
    return datetime.now(UTC)


def reschedule_today_departure(connection: sqlite3.Connection, policy: DeparturePolicy) -> None:
    """시각 설정을 바꾸면 오늘의 기록과 같은 트랜잭션에서 남은 예약을 맞춘다."""
    now = utc_now()
    day = now.astimezone(SEOUL).date()
    row = connection.execute(
        "SELECT payload,revision FROM day_records WHERE day=?", (day.isoformat(),)
    ).fetchone()
    if row is not None:
        record = decode_record(json.loads(row["payload"]))
        if record.day != day or record.revision != row["revision"]:
            raise SnapshotError("invalid_snapshot")
        enqueue_departure(connection, record, policy, now)


class SQLiteDayStore:
    """초기 버전부터 낙관적 잠금으로 중복 편집을 차단한다."""

    def __init__(
        self,
        database: Database,
        *,
        now: Callable[[], datetime] = utc_now,
        policy: DeparturePolicy | None = None,
        current_rules: Callable[[], WorkRules] | None = None,
    ) -> None:
        """날짜 전환도 쓰기 트랜잭션 안에서 검사할 수 있도록 시계를 받는다."""
        self._database = database
        self._now = now
        self._policy = policy
        self._current_rules = current_rules

    async def read(self, day: date) -> DayRecord:
        """조회는 실행·예약·파일을 생성하지 않는다."""
        rows = await self._database.fetch(
            "SELECT payload FROM day_records WHERE day=?", (day.isoformat(),)
        )
        if not rows:
            return self._new_record(day)
        payload = rows[0]["payload"]
        if not isinstance(payload, str):
            raise SnapshotError("storage_failed")
        try:
            record = decode_record(json.loads(payload))
            if record.day != day:
                raise ValueError
            return record
        except (ValueError, TypeError, KeyError):
            raise SnapshotError("storage_failed") from None

    async def read_month(self, first: date, last: date) -> tuple[WorkRecordItem, ...]:
        """월별 기록을 읽기 전용 연결 하나에서 가져온다."""
        return await asyncio.to_thread(self._read_month, first, last)

    def _read_month(self, first: date, last: date) -> tuple[WorkRecordItem, ...]:
        """조회로 미기록 행이나 알림을 만들지 않는다."""
        with self._database.transaction(read_only=True) as connection:
            rows = connection.execute(
                "SELECT day,payload FROM day_records WHERE day BETWEEN ? AND ? ORDER BY day DESC",
                (first.isoformat(), last.isoformat()),
            )
            records: list[WorkRecordItem] = []
            for row in rows:
                record = decode_record(json.loads(row["payload"]))
                if record.day.isoformat() != row["day"]:
                    raise SnapshotError("invalid_snapshot")
                # 메모·규칙도 검증하되 월 목록에는 표시값만 보관한다.
                records.append(record_item(record))
            return tuple(records)

    async def read_history(self, day: date) -> SavedRecordHistory:
        """현재 기록과 버전 목록이 다른 시점으로 섞이지 않게 한다."""
        return await asyncio.to_thread(self._read_history, day)

    def _read_history(self, day: date) -> SavedRecordHistory:
        """변경 전 비교용 한 건을 더 읽어 최근50회 차이를 보존한다."""
        with self._database.transaction(read_only=True) as connection:
            row = connection.execute(
                "SELECT payload FROM day_records WHERE day=?", (day.isoformat(),)
            ).fetchone()
            rows = connection.execute(
                "SELECT revision,payload FROM day_record_revisions WHERE day=? ORDER BY revision DESC LIMIT 51",
                (day.isoformat(),),
            )
            current = decode_record(json.loads(row["payload"])) if row else self._new_record(day)
            versions: list[WorkRecordItem] = []
            for stored in rows:
                version = decode_record(json.loads(stored["payload"]))
                if version.day != day or version.revision != stored["revision"]:
                    raise SnapshotError("invalid_snapshot")
                # 예정 계산·정정에는 현재 기록만 필요하고, 과거 버전은 표시값끼리 비교한다.
                versions.append(record_item(version))
            if (
                current.day != day
                or bool(row) != bool(versions)
                or (versions and versions[0].revision != current.revision)
            ):
                raise SnapshotError("invalid_snapshot")
            return SavedRecordHistory(current, tuple(versions))

    def _new_record(self, day: date) -> DayRecord:
        """최초 입력은 저장 전에도 현재 근무 규칙으로 미리 본다."""
        return DayRecord(day, rules=self._current_rules() if self._current_rules else None)

    @contextmanager
    def apply_today_rules(self, rules: WorkRules) -> Iterator[None]:
        """규칙 파일 교체와 직렬화해 오늘 기록·이력·남은 예약을 함께 갱신한다."""
        with self._database.transaction() as connection:
            now = self._now()
            day = now.astimezone(SEOUL).date()
            row = connection.execute(
                "SELECT revision,payload FROM day_records WHERE day=?", (day.isoformat(),)
            ).fetchone()
            # 출근을 입력하지 않은 날짜에는 설정 저장만으로 기록을 만들지 않는다.
            if row is not None:
                record = decode_record(json.loads(row["payload"]))
                if record.day != day or record.revision != row["revision"]:
                    raise SnapshotError("invalid_snapshot")
                if record.rules != rules:
                    record = self._write_revision(connection, replace(record, rules=rules), now)
                if self._policy is not None:
                    enqueue_departure(connection, record, self._policy, now)
            yield

    async def save(self, record: DayRecord, expected_revision: int) -> DayRecord:
        """기록·규칙·수정 이력을 같은 작업 스레드에서 확정한다."""
        return await asyncio.to_thread(self._save, record, expected_revision)

    async def correct(self, record: DayRecord, expected_revision: int) -> DayRecord:
        """기록 창의 명시적 정정에만 과거 날짜 수정을 허용한다."""
        return await asyncio.to_thread(self._save, record, expected_revision, allow_past=True)

    def _save(
        self, record: DayRecord, expected_revision: int, *, allow_past: bool = False
    ) -> DayRecord:
        """자정과 다른 창의 저장을 확인한 뒤 한 버전만 발행한다."""
        with self._database.transaction() as connection:
            today = self._now().astimezone(SEOUL).date()
            if record.day > today or (not allow_past and record.day != today):
                raise DayConflictError()
            row = connection.execute(
                "SELECT revision FROM day_records WHERE day=?", (record.day.isoformat(),)
            ).fetchone()
            current = row["revision"] if row else 0
            if current != expected_revision:
                raise DayConflictError()
            # 첫 입력이 설정 저장을 기다렸다면 쓰기 잠금 안에서 확정된 최신 규칙을 사용한다.
            if current == 0 and self._current_rules is not None:
                record = replace(record, rules=self._current_rules())
            saved = self._write_revision(connection, replace(record, revision=current), self._now())
            # 과거 정정은 기록만 보존하고 알림 큐에는 손대지 않는다.
            if self._policy is not None and record.day == today:
                enqueue_departure(connection, saved, self._policy, self._now())
            return saved

    def _write_revision(
        self, connection: sqlite3.Connection, record: DayRecord, now: datetime
    ) -> DayRecord:
        """현재 기록과 같은 버전의 수정 이력을 하나의 트랜잭션에 쓴다."""
        saved = replace(record, revision=record.revision + 1, updated_at=now.astimezone(UTC))
        payload = encode_record(saved)
        connection.execute(
            "INSERT INTO day_records(day,revision,payload) VALUES(?,?,?) ON CONFLICT(day) DO UPDATE SET revision=excluded.revision,payload=excluded.payload",
            (saved.day.isoformat(), saved.revision, payload),
        )
        connection.execute(
            "INSERT INTO day_record_revisions(day,revision,payload) VALUES(?,?,?)",
            (saved.day.isoformat(), saved.revision, payload),
        )
        return saved


def encode_record(record: DayRecord) -> str:
    """근무 규칙 스냅샷도 이력에 포함해 나중의 설정 변경과 구별한다."""
    return json.dumps(
        {
            "date": record.day.isoformat(),
            "revision": record.revision,
            "attendanceAt": record.attendance.astimezone(UTC).isoformat()
            if record.attendance
            else None,
            "leaveType": record.leave.value,
            "plan": record.plan,
            "rules": record.rules.payload() if record.rules else None,
            "updatedAt": record.updated_at.isoformat() if record.updated_at else None,
            "checkoutAt": record.checkout_at.astimezone(UTC).isoformat()
            if record.checkout_at
            else None,
            "overtime": record.overtime,
        },
        ensure_ascii=False,
    )


def decode_instant(value: object) -> datetime | None:
    """저장된 시점의 시간대 유무를 검사한다."""
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError
    instant = datetime.fromisoformat(value)
    if instant.tzinfo is None:
        raise ValueError
    return instant


def decode_record(value: object) -> DayRecord:
    """외부 JSON과 같은 타입 검사를 저장값에도 적용한다."""
    if not isinstance(value, dict):
        raise ValueError
    day, revision, plan = value["date"], value["revision"], value["plan"]
    if (
        not isinstance(day, str)
        or type(revision) is not int
        or revision < 1
        or not isinstance(plan, str)
        or len(plan) > 4000
    ):
        raise ValueError
    attendance = decode_instant(value["attendanceAt"])
    checkout = decode_instant(value.get("checkoutAt"))
    overtime = value.get("overtime", False)
    if type(overtime) is not bool:
        raise ValueError
    parsed_day = date.fromisoformat(day)
    if attendance is not None and attendance.astimezone(SEOUL).date() != parsed_day:
        raise ValueError
    if checkout is not None and (
        checkout.astimezone(SEOUL).date() != parsed_day
        or attendance is None
        or checkout < attendance
    ):
        raise ValueError
    return DayRecord(
        parsed_day,
        revision,
        attendance,
        parse_leave(value["leaveType"]),
        plan,
        parse_rules(value["rules"]),
        decode_instant(value["updatedAt"]),
        checkout,
        overtime,
    )
