"""외부 기술에 의존하지 않는 업무 값과 원천 매핑이다."""

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal

from src.domain.display_columns import DisplayColumn
from src.domain.source_alerts import SourceAlertRule

SourceKind = Literal["task", "meeting", "leave"]
EventType = Literal["meeting", "leave", "other"]


@dataclass(frozen=True)
class SourceMapping:
    """회사별 속성 ID와 업무 의미의 연결을 표현한다."""

    kind: SourceKind
    source_id: str
    properties: Mapping[str, str]
    approved_values: tuple[str, ...] = ()
    event_types: Mapping[str, EventType] = field(default_factory=dict)
    section: Literal["tasks", "qa"] = "tasks"
    round_title_property: str | None = None
    provider: str = "notion"
    flexible: bool = False
    default_event_type: EventType = "meeting"
    display_columns: tuple[DisplayColumn, ...] | None = None
    alert_rules: tuple[SourceAlertRule, ...] = ()
    stored_columns: tuple[DisplayColumn, ...] = ()
    calendar_column: DisplayColumn | None = None
    calendar_date_override: bool = False
    failure_rules: tuple[SourceAlertRule, ...] = ()
    event_alert_triggers: frozenset[str] = frozenset()

    classification_column: DisplayColumn | None = None
    classification_values: Mapping[str, str] = field(default_factory=dict)
    classification_default: str | None = None

    @property
    def has_person_scope(self) -> bool:
        """사람 칼럼을 비워 둔 사용자 정의 소스만 표 전체를 사용한다."""
        role = {"task": "assignees", "meeting": "participants", "leave": "people"}[self.kind]
        return not self.flexible or role in self.properties

    def allows_event_alert(self, trigger: str) -> bool:
        """소속 카테고리에 등록한 조건에 대해서만 알림을 예약한다."""
        return trigger in self.event_alert_triggers

    def collected_columns(self) -> dict[str, str]:
        """표시 또는 활성 알림 규칙에 쓰는 속성을 한 번씩 읽는다."""
        columns = {column.property_id: column.kind for column in self.storage_layout}
        columns.update(
            {rule.column_id: rule.column_kind for rule in self.alert_rules if rule.enabled}
        )
        return columns

    @property
    def storage_layout(self) -> tuple[DisplayColumn, ...]:
        """새 연결은 숨겨 둔 칼럼도 저장하고 이전 소스는 기존 저장 구성을 쓴다."""
        return self.stored_columns or self.display_columns or ()


@dataclass(frozen=True)
class Period:
    """날짜 전용 구간과 UTC 시점 구간을 구별한다."""

    kind: Literal["date", "instant"]
    start: str
    end: str | None

    def payload(self) -> dict[str, str | None]:
        """저장과 조회에서 같은 날짜·시각 구간 필드를 전달한다."""
        return {"kind": self.kind, "start": self.start, "end": self.end}


@dataclass(frozen=True)
class ItemBase:
    """업무 항목의 원천 근거와 관측 시각이다."""

    id: str
    source_id: str
    url: str
    source_edited_at: str
    observed_at: str
    display_values: Mapping[str, str] = field(default_factory=dict, kw_only=True, repr=False)
    alert_values: Mapping[str, str] = field(default_factory=dict, kw_only=True, repr=False)
    # 예약 시각은 같은 수집에서 큐에 저장하고 스냅샷에는 발송 전 대조할 해시만 남긴다.
    alert_due_at: Mapping[str, datetime] = field(default_factory=dict, kw_only=True, repr=False)
    calendar_periods: Mapping[str, Period] = field(default_factory=dict, kw_only=True, repr=False)


@dataclass(frozen=True)
class Task(ItemBase):
    """담당·상태·마감을 가진 업무다."""

    title: str
    assignee_ids: tuple[str, ...]
    status: str | None
    due: Period | None
    qa_round: str | None = None
    kind: Literal["task"] = field(default="task", init=False)


@dataclass(frozen=True)
class CalendarEvent(ItemBase):
    """원본 일정의 유형·취소 상태를 보존하며 저장 kind는 기존 값을 유지한다."""

    title: str
    participant_ids: tuple[str, ...]
    period: Period | None
    category: str | None = None
    event_type: EventType = "meeting"
    cancelled: bool = False
    kind: Literal["meeting"] = field(default="meeting", init=False)


@dataclass(frozen=True)
class Leave(ItemBase):
    """대상자·시간 구간·승인 근거를 가진 휴가다."""

    person_ids: tuple[str, ...]
    period: Period
    approval: str | None
    approved: bool
    kind: Literal["leave"] = field(default="leave", init=False)


type WorkItem = Task | CalendarEvent | Leave
