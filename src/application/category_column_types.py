"""카테고리의 값 종류와 현재 지원하는 원본 속성 타입을 연결한다."""

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
