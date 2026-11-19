"""소스별 화면·알림·캘린더 표시 선택이다."""

from dataclasses import dataclass, field

from src.domain.category_configuration import CategoryConfiguration
from src.domain.models import SourceKind

SOURCE_COLORS = ("blue", "green", "orange", "purple", "red", "teal")
SOURCE_SORTS = ("default", "date", "title")
SOURCE_CHANNELS = ("macos", "slack")


@dataclass(frozen=True)
class SourceCategory:
    """사용자 이름과 독립된 식별자로 여러 소스를 묶는다."""

    id: str
    name: str
    configuration: CategoryConfiguration | None = None

    @property
    def key(self) -> str:
        """표시·알림의 선택을 변경 가능한 이름과 분리한다."""
        return f"category:{self.id}"


def source_notifications(kind: SourceKind, section: str) -> tuple[tuple[str, str], ...]:
    """자료 유형에서 실제로 발생하는 알림 종류만 제공한다."""
    if kind == "task":
        prefix = "qa" if section == "qa" else "task"
        return (
            (f"{prefix}_assigned", "새 배정"),
            (f"{prefix}_status", "상태 변경"),
            (f"{prefix}_due", "기한 변경"),
            ("sync_error", "수집 오류"),
        )
    if kind == "meeting":
        return (("calendar", "새 일정"), ("meeting", "회의 10분 전"), ("sync_error", "수집 오류"))
    return (("sync_error", "수집 오류"),)


@dataclass(frozen=True)
class SourceDisplay:
    """원본 칼럼의 의미와 화면에서 사용하는 꾸밈을 구분한다."""

    id: str = field(repr=False)
    kind: SourceKind
    section: str
    name: str | None = None
    color: str | None = None
    sort: str = "default"
    channels: tuple[str, ...] | None = None
    url: str | None = field(default=None, repr=False)
    calendar_visible: bool = True
    notification_types: tuple[str, ...] | None = None
    category: SourceCategory | None = None
    flexible: bool = False
    events: tuple[tuple[str, str], ...] = ()

    @property
    def key(self) -> str | None:
        """카테고리를 지정한 소스만 화면이나 알림에 영역을 제공한다."""
        return self.category.key if self.category else None

    @property
    def title(self) -> str:
        """카드와 알림의 제목은 등록된 카테고리 이름으로 표시한다."""
        return self.category.name if self.category else ""


def display_groups(display: tuple[SourceDisplay, ...]) -> dict[str, tuple[SourceDisplay, ...]]:
    """같은 카테고리를 쓰는 표를 등록 순서대로 모은다."""
    grouped: dict[str, list[SourceDisplay]] = {}
    for item in display:
        if (key := item.key) is not None:
            grouped.setdefault(key, []).append(item)
    return {key: tuple(items) for key, items in grouped.items()}
