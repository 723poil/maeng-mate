"""사용자가 정한 원본 값 변경 알림의 조건과 식별자다."""

import hashlib
import re
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal, InvalidOperation
from typing import Literal
from zoneinfo import ZoneInfo

MAX_SOURCE_ALERT_RULES = 24


@dataclass(frozen=True)
class AlertDelivery:
    """규칙에 직접 입력한 본문과 발송할 채널이다."""

    message: str
    channels: tuple[str, ...]

    def __post_init__(self) -> None:
        """채널이 있는 규칙은 발신 앱이 표시할 수 있는 문구를 가져야 한다."""
        if self.channels and not self.message.strip():
            raise ValueError("선택한 채널에 보낼 알림 문구를 입력하세요.")
        if len(self.message) > 300 or any(ord(char) < 32 for char in self.message):
            raise ValueError("알림 문구를 한 줄로 300자 이내로 입력하세요.")


type ScheduleMode = Literal["previous_day", "same_day", "before_start"]


@dataclass(frozen=True)
class AlertSchedule:
    """원본 시작값의 서울 날짜 또는 시작 시점을 기준으로 예약한다."""

    mode: ScheduleMode
    value: str

    def __post_init__(self) -> None:
        """시각은 24시간제, 시작 전 간격은 음수가 아닌 분 단위로 받는다."""
        if self.mode == "before_start":
            if not re.fullmatch(r"(?:0|[1-9][0-9]{0,5})", self.value):
                raise ValueError("시작 몇 분 전인지 0~999999 사이 정수로 입력하세요.")
        elif not re.fullmatch(r"(?:[01][0-9]|2[0-3]):[0-5][0-9]", self.value):
            raise ValueError("서울 기준 알림 시각을 HH:MM 형식으로 입력하세요.")

    def due_at(self, start: str) -> datetime | None:
        """날짜만 있는 항목에는 시작 시각을 추정하지 않는다."""
        seoul = ZoneInfo("Asia/Seoul")
        if "T" in start:
            instant = datetime.fromisoformat(start)
            if instant.tzinfo is None:
                raise ValueError("시간대가 포함된 시작 시각이 필요합니다.")
            if self.mode == "before_start":
                return instant.astimezone(UTC) - timedelta(minutes=int(self.value))
            day = instant.astimezone(seoul).date()
        else:
            if self.mode == "before_start":
                return None
            day = date.fromisoformat(start)
        if self.mode == "previous_day":
            day -= timedelta(days=1)
        return datetime.combine(day, time.fromisoformat(self.value), seoul).astimezone(UTC)

    def expires_at(self, due: datetime) -> datetime:
        """날짜 알림은 서울 자정까지, 사전 알림은 시작 전까지 전달 기회를 유지한다."""
        if self.mode == "before_start":
            return due + timedelta(minutes=max(1, int(self.value)))
        seoul = ZoneInfo("Asia/Seoul")
        following_day = due.astimezone(seoul).date() + timedelta(days=1)
        return datetime.combine(following_day, time.min, seoul)


@dataclass(frozen=True)
class SourceAlertRule:
    """값 변경 전체 또는 특정 값으로의 변경을 감시한다."""

    id: str
    name: str
    column_id: str
    column_kind: str
    target: str | None = None
    enabled: bool = True
    time_only: bool = False
    delivery: AlertDelivery | None = None
    schedule: AlertSchedule | None = None

    @property
    def comparison_kind(self) -> str:
        """같은 속성의 날짜 조건과 시간 조건을 구별한다."""
        return "time" if self.time_only else self.column_kind

    @property
    def notification_kind(self) -> str:
        """알림 이름을 고쳐도 화면의 구독 선택을 유지한다."""
        return f"field:{self.id}"

    def accepts(self, digest: str) -> bool:
        """대상값 미지정은 모든 변경을 대상으로 한다."""
        return self.target is None or alert_value_key(self.target, self.comparison_kind) == digest


def alert_value_key(value: str, kind: str = "") -> str:
    """표시 길이에 상관없이 같은 원본 전체 값은 같은 비교값을 갖는다."""
    if kind == "number" and value:
        try:
            number = Decimal(value)
            if number.is_finite():
                value = "0" if not number else str(number.normalize())
        except InvalidOperation:
            pass
    if kind in {"date", "created_time", "last_edited_time"} and value:
        value = " ~ ".join(normalize_alert_date(part) for part in value.split(" ~ "))
    if kind in {"checkbox", "boolean"}:
        value = {"참": "예", "true": "예", "거짓": "아니요", "false": "아니요"}.get(
            value.casefold(), value
        )
    return hashlib.sha256(value.encode()).hexdigest()


def normalize_alert_date(value: str) -> str:
    """서울 시각의 표시와 사용자가 입력한 같은 날짜 표현을 맞춘다."""
    if "T" not in value and " " not in value:
        return value
    try:
        stamp = datetime.fromisoformat(value)
    except ValueError:
        return value
    if stamp.tzinfo is not None:
        stamp = stamp.astimezone(ZoneInfo("Asia/Seoul")).replace(tzinfo=None)
    return stamp.isoformat(sep=" ")


def is_source_alert_kind(value: str) -> bool:
    """환경설정에는 생성한 규칙의 식별자만 허용한다."""
    return re.fullmatch(r"field:[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}", value) is not None
