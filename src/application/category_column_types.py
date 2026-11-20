"""카테고리의 값 종류와 현재 지원하는 원본 속성 타입을 연결한다."""

from src.domain.main_list import ListFilter
from src.domain.source_scope import field_parts

NOTION_COLUMN_TYPES = {
    "text": (
        "title",
        "rich_text",
        "select",
        "multi_select",
        "status",
        "people",
        "url",
        "email",
        "phone_number",
    ),
    "number": ("number",),
    "date": ("date", "created_time", "last_edited_time"),
    "time": ("date", "created_time", "last_edited_time"),
    "boolean": ("checkbox",),
}
COLUMN_TYPES_BY_PROVIDER = {"notion": NOTION_COLUMN_TYPES}

LEGACY_CATEGORY_TYPES = {
    origin: kind
    for kind, origins in NOTION_COLUMN_TYPES.items()
    if kind != "time"
    for origin in origins
}


def accepts_column_type(provider: str, kind: str, source_kind: str) -> bool:
    """같은 값으로 정규화할 수 있는 원본 속성만 연결한다."""
    return source_kind in COLUMN_TYPES_BY_PROVIDER.get(provider, {}).get(kind, ())


def scope_property_kinds(condition: ListFilter) -> tuple[str, ...]:
    """저장 시점과 수집 시점에 같은 원본 속성 타입을 요구한다."""
    if condition.option_id is not None:
        return ("status", "select")
    kind, _ = field_parts(condition.field)
    return NOTION_COLUMN_TYPES["date" if kind == "period" else kind]
