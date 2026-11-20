"""이전 소스의 표시와 규칙을 소속 카테고리로 옮긴다."""

from collections.abc import Mapping
from dataclasses import replace
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
from src.domain.main_list import STATUS_GROUP_NAMES
from src.domain.models import SourceMapping
from src.domain.source_display import SourceCategory, SourceDisplay
from src.domain.task_display import TaskDisplayGroup, display_group

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


def _upgrade_classification_v2(payload: dict[str, object]) -> dict[str, object]:
    """카테고리 공통 규칙과 기존 역할별 분류를 실제 원본에 연결된 소스 규칙으로 옮긴다."""
    settings = parse_settings(payload)
    sources = source_documents(payload)
    for source, raw in zip(settings.sources, sources, strict=True):
        if "classification" in raw:
            continue
        column = source.classification_column
        if column:
            # 같은 카테고리에서도 원본 ID와 타입은 표마다 다르다.
            rule: dict[str, object] = {
                "property": column.property_id,
                "kind": column.kind,
                "name": column.label,
                "storageId": column.id,
                "values": dict(source.classification_values),
            }
            if source.classification_default is not None:
                rule["default"] = source.classification_default
        else:
            role = {"meeting": "category", "task": "status", "leave": "approval"}[source.kind]
            prop = source.properties.get(role)
            if prop is None:
                continue
            label = (
                attribute_name(raw, source, prop)
                or {"status": "상태", "category": "일정 유형", "approval": "승인 상태"}[role]
            )
            rule = {"property": prop, "name": label, "kind": "status_or_select"}
            if source.kind == "meeting":
                titles = {"meeting": "회의", "leave": "휴가", "other": "기타"}
                rule.update(
                    values={k: titles[v] for k, v in source.event_types.items()}, default="기타"
                )
            elif source.kind == "leave":
                rule["values"] = dict.fromkeys(source.approved_values, "승인")
            else:
                rule["values"] = dict.fromkeys(
                    settings.completed_statuses.get(source.source_id, ()), "완료"
                )
        raw["classification"] = rule
    categories = tuple(
        replace(
            item,
            configuration=replace(
                item.configuration,
                classification_column=None,
                classification_values=(),
                classification_default=None,
            ),
        )
        if item.configuration and item.configuration.classification_column
        else item
        for item in settings.categories
    )
    return {
        **payload,
        "classificationVersion": 2,
        "sources": sources,
        **({"categories": category_documents(categories)} if categories else {}),
    }


def _upgrade_classification_v3(
    payload: dict[str, object], status_options: Mapping[str, tuple[str, ...]]
) -> dict[str, object] | None:
    """기존 카드의 암묵적인 상태 분류를 수집 소스의 편집 가능한 값으로 이전한다."""
    if payload.get("classificationVersion") == 3:
        return None
    if payload.get("classificationVersion") != 2:
        payload = _upgrade_classification_v2(payload)
    settings = parse_settings(payload)
    sources = source_documents(payload)
    for source, style, raw in zip(settings.sources, settings.display, sources, strict=True):
        config = style.category.configuration if style.category else None
        if not (
            source.kind == "task"
            and config
            and config.main_list
            and "statusGroup" in config.main_list.fields
        ):
            continue
        prop = source.properties.get("status")
        if not prop:
            continue
        completed = settings.completed_statuses.get(source.source_id, ())
        rule = raw.get("classification")
        # v2의 자동 이전 형식만 식별한다. 직접 선택한 칼럼·이름·기본값과 해제한 연결을 보존한다.
        if not isinstance(rule, dict) or rule != {
            "property": prop,
            "name": attribute_name(raw, source, prop) or "상태",
            "kind": "status_or_select",
            "values": dict.fromkeys(completed, "완료"),
        }:
            continue
        values = dict.fromkeys(completed, "완료")
        for name in dict.fromkeys((*completed, *status_options.get(source.source_id, ()))):
            group = display_group(name)
            # 완료 체크는 숨김 조건에 보존하고, 알려진 상태의 정렬 그룹은 독립시킨다.
            if group != TaskDisplayGroup.OTHER or name not in values:
                values[name] = STATUS_GROUP_NAMES[group]
        raw["classification"] = {**rule, "values": values}
    return {**payload, "classificationVersion": 3, "sources": sources}


def upgrade_classification(
    payload: dict[str, object], status_options: Mapping[str, tuple[str, ...]]
) -> dict[str, object] | None:
    """기존 값별 매핑을 동일한 결과의 단일 조건 규칙으로 한 번 이전한다."""
    if payload.get("classificationVersion") == 4:
        return None
    payload = _upgrade_classification_v3(payload, status_options) or payload
    sources = source_documents(payload)
    for source in sources:
        old = source.get("classification")
        if isinstance(old, dict) and "values" in old:
            rule = dict(old)
            pairs = rule.pop("values")
            rule["rules"] = [
                {
                    "match": "all",
                    "result": result,
                    "conditions": [
                        {"property": rule["property"], "kind": rule["kind"], "value": key}
                    ],
                }
                for key, result in pairs.items()
            ]
            source["classification"] = rule
    return {**payload, "classificationVersion": 4, "sources": sources}
