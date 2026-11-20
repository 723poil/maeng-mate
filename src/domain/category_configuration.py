"""공유 저장 칼럼과 화면·알림 동작을 카테고리에서 정의한다."""

from dataclasses import dataclass

from src.domain.category_event_alerts import CategoryEventAlert
from src.domain.main_list import MainListRules
from src.domain.source_alerts import SourceAlertRule

CATEGORY_COLUMN_TYPES = ("text", "number", "date", "time", "boolean")


@dataclass(frozen=True)
class CategoryColumn:
    """프로바이더와 독립적인 저장 칼럼이다."""

    id: str
    name: str
    kind: str
    visible: bool = True
    legacy_kind: str | None = None
    status_badge: bool | None = None


@dataclass(frozen=True)
class ColumnBinding:
    """카테고리 칼럼에 연결한 원본 속성과 실제 타입이다."""

    property_id: str
    kind: str | None = None


@dataclass(frozen=True)
class CategoryAlert:
    """같은 카테고리 칼럼의 조건을 연결된 표에서 평가한다."""

    rule: SourceAlertRule
    sources: tuple[str, ...] | None = None


@dataclass(frozen=True)
class CategorySourceOptions:
    """이전 소스별 설정을 잃지 않도록 카테고리 안에만 보관한다."""

    source_id: str
    calendar_visible: bool | None = None
    color: str | None = None
    channels: tuple[str, ...] | None = None
    notification_types: tuple[str, ...] | None = None
    custom_display: bool | None = None
    hidden_columns: tuple[str, ...] = ()
    column_order: tuple[str, ...] | None = None


@dataclass(frozen=True)
class CategoryConfiguration:
    """저장 칼럼, 캘린더 표시와 값 변경 알림의 기준이다."""

    columns: tuple[CategoryColumn, ...] = ()
    calendar_visible: bool = True
    calendar_color: str | None = None
    calendar_date: str | None = None
    channels: tuple[str, ...] | None = None
    alerts: tuple[CategoryAlert, ...] = ()
    inherited: tuple[CategorySourceOptions, ...] = ()
    event_alerts: tuple[CategoryEventAlert, ...] | None = ()
    main_list: MainListRules | None = None
    classification_column: str | None = None
    classification_values: tuple[tuple[str, str], ...] = ()
    classification_default: str | None = None
