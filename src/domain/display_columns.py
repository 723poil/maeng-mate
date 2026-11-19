"""원본 칼럼을 사용자가 지정한 이름과 순서로 표시한다."""

from collections.abc import Mapping
from dataclasses import dataclass

DISPLAY_COLUMN_TYPES = (
    "title",
    "rich_text",
    "number",
    "select",
    "multi_select",
    "status",
    "date",
    "people",
    "checkbox",
    "url",
    "email",
    "phone_number",
    "created_time",
    "last_edited_time",
)
TITLE_COLUMN_TYPES = ("title", "rich_text", "select", "status", "number")
MAX_DISPLAY_COLUMNS = 24
DISPLAY_VALUE_LIMIT = 240


@dataclass(frozen=True)
class DisplayColumn:
    """원본 칼럼 식별자와 앱에서 보여 줄 이름이다."""

    id: str
    label: str
    kind: str
    origin: str | None = None
    time_only: bool = False
    full_text: bool = False
    status_badge: bool | None = None

    @property
    def shows_status_badge(self) -> bool:
        """자동 표시는 이름에 관계없이 원본 상태 타입을 따르고 직접 고른 방식을 우선한다."""
        return self.kind == "status" if self.status_badge is None else self.status_badge

    @property
    def property_id(self) -> str:
        """카테고리 칼럼을 연결한 경우 원본 속성 ID를 반환한다."""
        return self.origin or self.id

    @property
    def value_key(self) -> str:
        """같은 카테고리 칼럼이라도 연결을 바꾸면 이전 값을 재사용하지 않는다."""
        return f"{self.id}:{self.origin}" if self.origin is not None else self.id


def labelled_values(
    columns: tuple[DisplayColumn, ...], values: Mapping[str, str]
) -> tuple[tuple[str, str], ...]:
    """저장된 값에 현재 표시 이름과 순서를 적용한다."""
    # 필터용 전체 텍스트를 저장한 칼럼도 메뉴바와 캘린더에는 미리보기만 전달한다.
    return tuple(
        (column.label, values.get(column.value_key, "")[:DISPLAY_VALUE_LIMIT]) for column in columns
    )


def display_line(title: str, fields: tuple[tuple[str, str], ...]) -> str:
    """메뉴바와 설정 미리보기에 같은 칼럼 순서를 적용한다."""
    return "   ·   ".join(
        (title or "제목 없음", *(f"{name} {value or '—'}" for name, value in fields))
    )
