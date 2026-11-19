"""기존 화면의 그룹을 한 번 사용자 관리 카테고리로 옮긴다."""

from dataclasses import replace
from uuid import uuid4

from src.application.work_settings import (
    Settings,
    canonical_sources,
    category_documents,
    parse_settings,
    source_documents,
)
from src.domain.calendar import CalendarKind
from src.domain.preferences import LEGACY_SECTIONS, Preferences, SectionPreference
from src.domain.source_display import SourceCategory

LEGACY_CATEGORY_NAMES = {"tasks": "업무", "qa": "QA", "meetings": "회의"}


def upgrade_legacy_categories(
    payload: dict[str, object],
) -> tuple[dict[str, object], dict[str, str]] | None:
    """실제로 있던 기존 그룹만 현재 문서의 카테고리로 등록한다."""
    if payload.get("categoryAssignmentVersion") == 1:
        return None
    sources = canonical_sources(source_documents(payload))
    if not sources:
        return None  # 새 설치에는 빈 설정을 그대로 두어 기존 파일을 가져올 수 있다.
    settings = parse_settings(payload)
    categories = list(settings.categories)
    by_name = {item.name.casefold(): item for item in reversed(categories)}
    keys: dict[str, str] = {}
    for raw, mapping in zip(sources, settings.sources, strict=True):
        if "categoryId" in raw or mapping.flexible or mapping.kind == "leave":
            continue
        group = "meetings" if mapping.kind == "meeting" else mapping.section
        label = LEGACY_CATEGORY_NAMES[group]
        # 사용자가 정한 다른 소스명과 이미 고른 카테고리는 그대로 보존한다.
        if raw.get("name") not in (None, label):
            continue
        category = by_name.get(label.casefold())
        if category is None:
            category = SourceCategory(str(uuid4()), label)
            by_name[label.casefold()] = category
            categories.append(category)
        raw["categoryId"] = category.id
        previous_key = (
            f"source:{mapping.source_id}"
            if raw.get("name") or raw.get("sort", "default") != "default"
            else group
        )
        keys[previous_key] = category.key
    return (
        {
            **payload,
            "categories": category_documents(tuple(categories)),
            "sources": list(sources),
            "categoryAssignmentVersion": 1,
        },
        keys,
    )


def upgrade_category_preferences(
    value: Preferences, keys: dict[str, str], settings: Settings
) -> Preferences:
    """이전 그룹에서 지정한 표시와 알림을 같은 새 카테고리에 연결한다."""
    if value.revision == 0:
        # 설정 행이 없던 설치는 당시 기본 그룹 순서에서 새 카테고리 키로 옮긴다.
        value = Preferences(
            sections=(
                SectionPreference("work"),
                *(SectionPreference(key) for key in LEGACY_SECTIONS),
                SectionPreference("calendar", False),
            )
        )
    explicit = {item.key for item in value.sections}
    seen: set[str] = set()
    order = []
    for item in value.sections:
        key = keys.get(item.key, item.key)
        if key in LEGACY_SECTIONS or key in seen or (key != item.key and key in explicit):
            continue
        order.append(SectionPreference(key, item.visible))
        seen.add(key)
    muted = {
        key: kinds
        for key, kinds in value.muted_source_notifications.items()
        if key not in LEGACY_SECTIONS and key not in keys
    }
    for legacy, current in keys.items():
        if legacy in value.muted_source_notifications:
            muted.setdefault(current, value.muted_source_notifications[legacy])
    kinds = category_calendar_kinds(settings)
    hidden = set(value.calendar.hidden_categories)
    hidden.update(
        key
        for key, values in kinds.items()
        if key not in value.calendar.shown_categories
        and not values.intersection(value.calendar.visible_kinds)
    )
    return replace(
        value,
        revision=value.revision + 1,
        sections=tuple(order),
        muted_source_notifications=muted,
        calendar=replace(value.calendar, hidden_categories=tuple(sorted(hidden))),
    )


def category_calendar_kinds(settings: Settings) -> dict[str, set[CalendarKind]]:
    """기존에 숨긴 자료 유형을 연결된 카테고리 범위와 대조한다."""
    kinds: dict[str, set[CalendarKind]] = {}
    for source, display in zip(settings.sources, settings.display, strict=True):
        if display.category is None or not display.calendar_visible:
            continue
        values = kinds.setdefault(display.category.key, set())
        if source.kind == "meeting":
            if "category" in source.properties:
                values.update(source.event_types.values())
                values.add("other")
            else:
                values.add(source.default_event_type)
        else:
            values.add("qa" if source.section == "qa" else source.kind)
    return kinds
