"""이전 소스의 표시와 규칙을 소속 카테고리로 옮긴다."""

from dataclasses import replace
from urllib.parse import unquote
from uuid import UUID, uuid5

from src.application.category_column_types import LEGACY_CATEGORY_TYPES
from src.application.work_settings import (
    Settings,
    canonical_sources,
    category_bindings,
    category_documents,
    column_bindings_payload,
    parse_settings,
    source_documents,
)
from src.domain.category_configuration import (
    CategoryAlert,
    CategoryColumn,
    CategoryConfiguration,
    CategorySourceOptions,
    ColumnBinding,
)
from src.domain.category_event_alerts import EVENT_ALERT_KINDS, CategoryEventAlert
from src.domain.display_columns import DisplayColumn
from src.domain.models import SourceMapping
from src.domain.source_display import SourceCategory, SourceDisplay

FORMER_CATEGORY_KEYS = (
    "displayColumns",
    "calendarVisible",
    "color",
    "notificationChannels",
    "notificationTypes",
    "alertRules",
)


def upgrade_category_configuration(payload: dict[str, object]) -> dict[str, object] | None:
    """같은 이름·타입의 칼럼은 공유하고 나머지는 정확한 연결을 유지한다."""
    settings = parse_settings(payload)
    if not settings.categories or all(
        item.configuration is not None for item in settings.categories
    ):
        return None
    sources = canonical_sources(source_documents(payload))
    categories = tuple(
        item if item.configuration is not None else upgrade_one(item, settings, sources)
        for item in settings.categories
    )
    return {**payload, "categories": category_documents(categories), "sources": list(sources)}


def upgrade_category_event_alerts(payload: dict[str, object]) -> dict[str, object] | None:
    """예전 소스에서 명시적으로 선택한 알림만 카테고리 규칙으로 이전한다."""
    settings = parse_settings(payload)
    categories = []
    changed = False
    for item in settings.categories:
        current = item.configuration
        if current is None or current.event_alerts is not None:
            categories.append(item)
            continue
        registered: dict[str, CategoryEventAlert] = {}
        for style in settings.display:
            if style.key != item.key or style.notification_types is None:
                continue
            for kind, title in style.events:
                trigger = EVENT_ALERT_KINDS.get(kind)
                if trigger is not None and kind in style.notification_types:
                    registered.setdefault(trigger, CategoryEventAlert(trigger, title))
        categories.append(
            replace(item, configuration=replace(current, event_alerts=tuple(registered.values())))
        )
        changed = True
    return {**payload, "categories": category_documents(tuple(categories))} if changed else None


def upgrade_one(
    category: SourceCategory, settings: Settings, sources: tuple[dict[str, object], ...]
) -> SourceCategory:
    """카테고리에 연결된 예전 표를 하나의 공유 스키마로 전환한다."""
    members = tuple(
        (mapping, style, raw)
        for mapping, style, raw in zip(settings.sources, settings.display, sources, strict=True)
        if style.category is not None and style.category.id == category.id
    )
    return merge_members(category, members)


def merge_members(
    category: SourceCategory,
    members: tuple[tuple[SourceMapping, SourceDisplay, dict[str, object]], ...],
) -> SourceCategory:
    """기존 카테고리에도 아직 분류되지 않은 소스의 설정을 가져올 수 있다."""
    base = category.configuration
    styles = tuple(style for _, style, _ in members)
    visible = (
        base.calendar_visible
        if base is not None
        else not styles or any(item.calendar_visible for item in styles)
    )
    color = base.calendar_color if base is not None else shared_color(styles)
    channels = base.channels if base is not None else shared_channels(styles)
    columns = list(base.columns if base is not None else ())
    locked = frozenset(item.id for item in columns)
    alerts = list(base.alerts if base is not None else ())
    previous: list[CategorySourceOptions] = []
    for mapping, style, raw in members:
        links = category_bindings(raw)
        hidden: set[str] = set()
        order = []
        for column in mapping.display_columns or ():
            order.append(
                attach_column(category, columns, links, column, visible=True, locked=locked)
            )
        for rule in mapping.alert_rules:
            kind = LEGACY_CATEGORY_TYPES[rule.column_kind]
            matched = next(
                (
                    item.id
                    for item in columns
                    if (binding := links.get(item.id)) is not None
                    and binding.property_id == rule.column_id
                    and item.kind == kind
                ),
                None,
            )
            if matched is None:
                name = attribute_name(raw, mapping, rule.column_id) or rule.name
                matched = attach_column(
                    category,
                    columns,
                    links,
                    DisplayColumn(rule.column_id, name[:80], rule.column_kind),
                    visible=False,
                )
                hidden.add(matched)
            alerts.append(
                CategoryAlert(
                    replace(rule, column_id=matched, column_kind=kind), (mapping.source_id,)
                )
            )
        if links:
            raw["columnMappings"] = column_bindings_payload(links)
        option = CategorySourceOptions(
            mapping.source_id,
            False if visible and not style.calendar_visible else None,
            style.color if style.color != color else None,
            style.channels if style.channels != channels else None,
            style.notification_types,
            True if mapping.display_columns is not None else None,
            tuple(sorted(hidden)),
            tuple(order) if order else None,
        )
        if option != CategorySourceOptions(mapping.source_id):
            previous.append(option)
        for key in FORMER_CATEGORY_KEYS:
            raw.pop(key, None)
    previous = [
        replace(item, column_order=None)
        if tuple(column.id for column in columns if column.id in (item.column_order or ()))
        == item.column_order
        else item
        for item in previous
    ]
    member_ids = {mapping.source_id for mapping, _, _ in members}
    untouched = tuple(
        item
        for item in (base.inherited if base is not None else ())
        if item.source_id not in member_ids
    )
    return replace(
        category,
        configuration=CategoryConfiguration(
            tuple(columns),
            visible,
            color,
            base.calendar_date if base is not None else None,
            channels,
            tuple(alerts),
            (*untouched, *previous),
            base.event_alerts if base is not None else None,
            base.main_list if base is not None else None,
            base.classification_column if base else None,
            base.classification_values if base else (),
            base.classification_default if base else None,
        ),
    )


def shared_color(styles: tuple[SourceDisplay, ...]) -> str | None:
    """모든 소스가 같은 색이면 카테고리 기본색으로 승계한다."""
    return (
        styles[0].color
        if styles and all(item.color == styles[0].color for item in styles)
        else None
    )


def shared_channels(styles: tuple[SourceDisplay, ...]) -> tuple[str, ...] | None:
    """일치하는 전달 선택만 공유 채널로 승계한다."""
    return (
        styles[0].channels
        if styles and all(item.channels == styles[0].channels for item in styles)
        else None
    )


def attach_column(
    category: SourceCategory,
    columns: list[CategoryColumn],
    links: dict[str, ColumnBinding],
    value: DisplayColumn,
    *,
    visible: bool,
    locked: frozenset[str] = frozenset(),
) -> str:
    """같은 표에서 서로 다른 속성은 이름이 같아도 같은 칸으로 합치지 않는다."""
    kind = LEGACY_CATEGORY_TYPES[value.kind]
    binding = ColumnBinding(value.id, value.kind)
    for index, item in enumerate(columns):
        if (
            item.name == value.label
            and item.kind == kind
            and links.get(item.id, binding).property_id == value.id
            and (not visible or item.visible or item.id not in locked)
        ):
            links[item.id] = binding
            if visible and not item.visible:
                columns[index] = replace(item, visible=True)
            return item.id
    identifier = str(uuid5(UUID(category.id), f"{value.kind}:{value.label}:{len(columns)}"))
    columns.append(CategoryColumn(identifier, value.label, kind, visible))
    links[identifier] = binding
    return identifier


def move_unassigned_settings(
    settings: Settings,
    stored: tuple[dict[str, object], ...],
    sources: tuple[dict[str, object], ...],
) -> tuple[SourceCategory, ...]:
    """아직 분류되지 않았던 소스가 연결될 때도 이전 설정을 소유 카테고리로 이동한다."""
    unassigned = {
        mapping.source_id: (mapping, style)
        for mapping, style, raw in zip(settings.sources, settings.display, stored, strict=True)
        if style.category is None and raw.keys() & set(FORMER_CATEGORY_KEYS)
    }
    if not unassigned:
        return settings.categories
    groups: dict[str, list[tuple[SourceMapping, SourceDisplay, dict[str, object]]]] = {}
    for raw in sources:
        category_id, source_id = raw.get("categoryId"), raw.get("dataSourceId")
        if not isinstance(category_id, str) or not isinstance(source_id, str):
            continue
        try:
            identifier = str(UUID(source_id))
        except ValueError:
            identifier = source_id
        if identifier in unassigned:
            mapping, style = unassigned[identifier]
            groups.setdefault(category_id, []).append((mapping, style, raw))
    return tuple(
        merge_members(category, tuple(groups[category.id])) if category.id in groups else category
        for category in settings.categories
    )


def attribute_name(raw: dict[str, object], mapping: SourceMapping, property_id: str) -> str | None:
    """예전 원본 속성 이름이 기록돼 있으면 숨겨 둔 칸에도 재사용한다."""
    labels = raw.get("columnLabels")
    if not isinstance(labels, dict):
        return None
    for role, identifier in mapping.properties.items():
        if identifier == property_id and isinstance(name := labels.get(role), str):
            return name
    return None


def upgrade_classification(payload: dict[str, object]) -> dict[str, object] | None:
    """일정 유형으로 명시한 원본 속성을 카테고리의 편집 가능한 기준으로 한 번 옮긴다."""
    if payload.get("classificationVersion") == 1:
        return None
    settings = parse_settings(payload)
    sources = source_documents(payload)
    categories = []
    for category in settings.categories:
        config = category.configuration
        members = [
            (source, raw)
            for source, raw in zip(settings.sources, sources, strict=True)
            if raw.get("categoryId") == category.id
            and source.kind == "meeting"
            and "category" in source.properties
        ]
        if not members or config is None or config.classification_column:
            categories.append(category)
            continue
        # 서로 다른 의미를 하나의 공유 규칙으로 덮지 않고 기존 소스별 설정을 유지한다.
        if any(source.event_types != members[0][0].event_types for source, _ in members):
            categories.append(category)
            continue
        matches = [
            {
                key
                for key, binding in category_bindings(raw).items()
                if unquote(binding.property_id) == unquote(source.properties["category"])
            }
            for source, raw in members
        ]
        shared = set.intersection(*matches)
        # 기존 기준이 없던 소스에 우연히 같은 저장 칼럼이 연결돼 있으면 별도 칼럼을 쓴다.
        other_links = {
            key
            for source, raw in zip(settings.sources, sources, strict=True)
            if raw.get("categoryId") == category.id
            and not (source.kind == "meeting" and "category" in source.properties)
            for key in category_bindings(raw)
        }
        columns = config.columns
        key = next(
            (col.id for col in columns if col.kind == "text" and col.id in shared - other_links),
            None,
        )
        inherited = list(config.inherited)
        if key is None:
            first, raw = members[0]
            name = attribute_name(raw, first, first.properties["category"]) or "일정 유형"
            key = str(uuid5(UUID(category.id), "migrated-event-classification"))
            columns = (*columns, CategoryColumn(key, name[:80], "text", visible=False))
            for source, raw in members:
                links = category_bindings(raw)
                source_id = source.properties["category"]
                binding = next(
                    (
                        value
                        for value in links.values()
                        if unquote(value.property_id) == unquote(source_id)
                    ),
                    ColumnBinding(source_id, "status_or_select"),
                )
                links[key] = (
                    binding if binding.kind else ColumnBinding(source_id, "status_or_select")
                )
                raw["columnMappings"] = column_bindings_payload(links)
                # 숨은 이전용 칼럼을 추가해도 기존 목록의 기본 표시 모드는 유지한다.
                option = next(
                    (item for item in inherited if item.source_id == source.source_id), None
                )
                if option is None:
                    inherited.append(CategorySourceOptions(source.source_id, hidden_columns=(key,)))
                else:
                    inherited[inherited.index(option)] = replace(
                        option, hidden_columns=(*option.hidden_columns, key)
                    )
        names = {"meeting": "회의", "leave": "휴가", "other": "기타"}
        rules = tuple((value, names[result]) for value, result in members[0][0].event_types.items())
        categories.append(
            replace(
                category,
                configuration=replace(
                    config,
                    columns=columns,
                    classification_column=key,
                    classification_values=rules,
                    classification_default="기타",
                    inherited=tuple(inherited),
                ),
            )
        )
    return {
        **payload,
        "classificationVersion": 1,
        "sources": sources,
        **({"categories": category_documents(tuple(categories))} if categories else {}),
    }
