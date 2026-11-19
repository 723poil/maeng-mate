"""사용자가 정한 원본 값 변경 알림의 조건과 식별자다."""

import hashlib
import re
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo

MAX_SOURCE_ALERT_RULES = 24


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
