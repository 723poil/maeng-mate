"""소스의 속성 연결과 표시 설정을 읽고 카테고리 기준을 적용한다."""

from dataclasses import replace

from src.application.alert_settings_codec import parse_alert_rules
from src.application.category_column_types import accepts_column_type
from src.application.category_settings_codec import category_bindings, inherited_options
from src.application.notion_links import notion_link
from src.application.settings_validation import nonblank
from src.domain.category_event_alerts import EVENT_ALERT_KINDS
from src.domain.display_columns import DISPLAY_COLUMN_TYPES, MAX_DISPLAY_COLUMNS, DisplayColumn
from src.domain.event_classification import EVENT_CLASSIFICATION
from src.domain.models import ClassificationCondition, ClassificationRule, EventType, SourceMapping
from src.domain.source_alerts import COLLECTION_STATE, SourceAlertRule
from src.domain.source_display import (
    SOURCE_CHANNELS,
    SOURCE_COLORS,
    SOURCE_SORTS,
    SourceCategory,
    SourceDisplay,
    source_notifications,
)
from src.domain.source_roles import ROLE_TEMPLATES, source_roles
from src.domain.source_scope import calendar_conditions
from src.domain.work_dates import canonical_identity


def parse_classification_rules(raw: object, provider: str) -> tuple[ClassificationRule, ...]:
    """앞선 규칙부터 적용하도록 입력 순서를 보존하고 빈 규칙을 거부한다."""
    if not isinstance(raw, list):
        raise ValueError
    rules = []
    for row in raw:
        if not isinstance(row, dict) or set(row) != {"match", "conditions", "result"}:
            raise ValueError
        mode = nonblank(row["match"])
        items = row["conditions"]
        if mode not in ("all", "any") or not isinstance(items, list) or not items:
            raise ValueError
        conditions = []
        for item in items:
            if not isinstance(item, dict) or set(item) != {"property", "kind", "value"}:
                raise ValueError
            kind = nonblank(item["kind"])
            if kind != "status_or_select" and not accepts_column_type(provider, "text", kind):
                raise ValueError
            value = item["value"]
            if not isinstance(value, str):
                raise ValueError
            conditions.append(ClassificationCondition(nonblank(item["property"]), kind, value))
        rules.append(ClassificationRule(mode, tuple(conditions), nonblank(row["result"])))
    return tuple(rules)


def parse_source(item: dict[str, object]) -> SourceMapping:
    """소스 종류별 필수 매핑을 검증하고 UUID 표기를 통일한다."""
    kind = item.get("kind")
    if kind not in ("task", "meeting", "leave"):
        raise ValueError
    allowed = {"kind", "dataSourceId", "properties"}
    provider = nonblank(item.get("provider", "notion"))
    if provider != "notion":
        raise ValueError
    if "provider" in item:
        allowed.add("provider")
    flexible = item.get("flexible", False)
    if type(flexible) is not bool:
        raise ValueError
    if "flexible" in item:
        allowed.add("flexible")
    event_default = item.get("defaultEventType", "meeting")
    if event_default not in ("meeting", "other", "leave"):
        raise ValueError
    if "defaultEventType" in item:
        if kind != "meeting":
            raise ValueError
        allowed.add("defaultEventType")
    if "name" in item:
        allowed.add("name")
        if len(nonblank(item["name"])) > 120:
            raise ValueError
    if "pageUrl" in item:
        allowed.add("pageUrl")
        if notion_link(item["pageUrl"]) is None:
            raise ValueError
    if kind == "leave":
        allowed.add("approvedValues")
    if kind == "task" and "completedStatuses" in item:
        allowed.add("completedStatuses")
    if kind == "meeting" and "eventTypes" in item:
        allowed.add("eventTypes")
    section = item.get("section", "tasks")
    if "section" in item:
        allowed.add("section")
    if section not in {"tasks", "qa"} or (section == "qa" and kind != "task"):
        raise ValueError
    legacy = "roundTitleProperty" in item
    parent_key = "roundTitleProperty" if legacy else "parentTitleProperty"
    nested = parent_key in item
    # 이전 하위 표는 기존 제목 표시와 첫 숫자 정렬을 그대로 복원한다.
    parent_label = item.get("parentLabel", legacy)
    parent_order = item.get("parentOrder", "legacy" if legacy else "none")
    number_property = item.get("parentNumberProperty")
    if type(parent_label) is not bool or parent_order not in ("none", "legacy", "suffix", "column"):
        raise ValueError
    if (parent_order == "column") != ("parentNumberProperty" in item):
        raise ValueError
    if nested:
        allowed.update(
            key for key in ("parentLabel", "parentOrder", "parentNumberProperty") if key in item
        )
    if nested:
        allowed.add(parent_key)
        if kind != "task":
            raise ValueError
    allowed.update(
        key
        for key in (
            "color",
            "sort",
            "sortDirection",
            "sourceId",
            "notificationChannels",
            "notificationTypes",
            "calendarVisible",
            "category",
            "categoryId",
            "columnRoles",
            "columnLabels",
            "displayColumns",
            "alertRules",
            "columnMappings",
            "classification",
        )
        if key in item
    )
    if set(item) != allowed:
        raise ValueError
    origin_id = canonical_identity(nonblank(item["dataSourceId"]))
    # 이전 문서는 원본 ID가 곧 논리 ID다. 새 연결만 별도 키를 갖는다.
    source_id = canonical_identity(nonblank(item.get("sourceId", origin_id)))
    raw = item["properties"]
    if not isinstance(raw, dict):
        raise ValueError
    properties = {nonblank(key): nonblank(value) for key, value in raw.items()}
    if "columnMappings" in item:
        category_bindings(item)
    if "columnLabels" in item:
        labels = item["columnLabels"]
        if not isinstance(labels, dict) or set(labels) - properties.keys():
            raise ValueError
        if any(len(nonblank(value)) > 250 for value in labels.values()):
            raise ValueError
    roles = source_roles(kind, item.get("columnRoles"))
    required_keys = {role.key for role in roles if role.required}
    if flexible and kind in {"task", "meeting"}:
        required_keys -= {"assignees", "status", "participants"}
    if not required_keys <= properties.keys() or properties.keys() - {role.key for role in roles}:
        raise ValueError
    builtin = {role.key for role in ROLE_TEMPLATES[kind]}
    custom_columns = tuple(
        DisplayColumn(role.key, role.title, role.types[0], properties[role.key])
        for role in roles
        if role.key not in builtin and role.key in properties
    )
    approved: tuple[str, ...] = ()
    if kind == "leave":
        values = item["approvedValues"]
        if not isinstance(values, list) or not values:
            raise ValueError
        approved = tuple(nonblank(value) for value in values)
    event_types: dict[str, EventType] = {}
    if kind == "meeting":
        if ("category" in properties) != ("eventTypes" in item):
            raise ValueError
        if "eventTypes" in item:
            event_types = read_event_types(item["eventTypes"])
    classification = item.get("classification")
    column = None
    classes: dict[str, str] = {}
    fallback = None
    rules: tuple[ClassificationRule, ...] = ()
    if "classification" in item:
        if not isinstance(classification, dict) or set(classification) - {
            "default",
            "storageId",
        } not in ({"property", "kind", "name", "values"}, {"property", "kind", "name", "rules"}):
            raise ValueError
        prop = nonblank(classification["property"])
        origin_kind = nonblank(classification["kind"])
        if origin_kind != "status_or_select" and not accepts_column_type(
            provider, "text", origin_kind
        ):
            raise ValueError
        storage_id = (
            nonblank(classification["storageId"]) if "storageId" in classification else prop
        )
        column = DisplayColumn(
            storage_id,
            nonblank(classification["name"]),
            origin_kind,
            prop if "storageId" in classification else None,
        )
        rules = parse_classification_rules(classification.get("rules", []), provider)
        pairs = classification.get("values", {})
        if not isinstance(pairs, dict):
            raise ValueError
        for key, result in pairs.items():
            name = nonblank(key)
            if name in classes:
                raise ValueError
            classes[name] = nonblank(result)
        if "default" in classification:
            fallback = nonblank(classification["default"])
    return SourceMapping(
        kind,
        source_id,
        properties,
        approved,
        event_types,
        "qa" if section == "qa" else "tasks",
        nonblank(item[parent_key]) if nested else None,
        provider,
        flexible,
        "other" if event_default == "other" else "leave" if event_default == "leave" else "meeting",
        parse_display_columns(item["displayColumns"], status_property=properties.get("status"))
        if "displayColumns" in item
        else None,
        parse_alert_rules(item["alertRules"]) if "alertRules" in item else (),
        origin_id=origin_id,
        parent_label=parent_label,
        parent_order=str(parent_order),
        parent_number_property=nonblank(number_property) if parent_order == "column" else None,
        role_columns=custom_columns,
        classification_column=column,
        classification_values=classes,
        classification_rules=rules,
        classification_default=fallback,
    )


def parse_display_columns(
    raw: object, *, status_property: str | None = None
) -> tuple[DisplayColumn, ...]:
    """표시할 칼럼의 타입·이름·중복을 검증한다."""
    if not isinstance(raw, list) or len(raw) > MAX_DISPLAY_COLUMNS:
        raise ValueError
    result = []
    for item in raw:
        if not isinstance(item, dict) or set(item) != {"id", "label", "kind"}:
            raise ValueError
        identifier, label, kind = map(nonblank, (item["id"], item["label"], item["kind"]))
        if (
            kind not in DISPLAY_COLUMN_TYPES
            or len(label) > 80
            or any(ord(char) < 32 for char in label)
        ):
            raise ValueError
        # 카테고리 도입 전 표시 설정도 업무 상태 역할을 유지한다.
        result.append(
            DisplayColumn(
                identifier,
                label,
                kind,
                status_badge=True if identifier == status_property else None,
            )
        )
    if len({item.id for item in result}) != len(result):
        raise ValueError
    return tuple(result)


def display_columns_payload(columns: tuple[DisplayColumn, ...]) -> list[dict[str, str]]:
    """선택 순서대로 원본 식별자와 표시명을 저장한다."""
    return [{"id": item.id, "label": item.label, "kind": item.kind} for item in columns]


def bind_scope(rule: SourceAlertRule, source: SourceMapping) -> SourceAlertRule | None:
    """연결한 표의 조건만 실행하며 같은 의미의 이전 예약은 같은 키로 이어받는다."""
    if rule.source_scopes is None:
        return rule
    scope = rule.scope_for(source.source_id)
    if scope is None:
        return None
    if calendar_conditions(source, "meeting") == scope.conditions:
        return replace(rule, source_scopes=(scope,), item_filter="meeting")
    if calendar_conditions(source, "upcoming") == scope.conditions:
        return replace(rule, source_scopes=(scope,), item_filter="upcoming")
    return replace(rule, source_scopes=(scope,), item_filter=None)


def resolve_category_mapping(
    source: SourceMapping, raw: dict[str, object], category: SourceCategory | None
) -> SourceMapping:
    """공유 칸의 설정을 현재 표에 연결된 원본 속성으로 조립한다."""
    config = category.configuration if category is not None else None
    if config is None:
        return source
    links = category_bindings(raw)
    if links.keys() - {item.id for item in config.columns}:
        raise ValueError
    rule_fields = config.main_list.fields if config.main_list is not None else set()
    sort = raw.get("sort")
    if isinstance(sort, str) and sort.startswith("column:"):
        rule_fields.add(sort.removeprefix("column:"))
    # 업무 분류가 비교 대상이면 화면에 보이지 않는 긴 분류 이름도 전체 값으로 비교한다.
    classification_rule = source.kind == "task" and "statusGroup" in rule_fields
    if classification_rule and source.classification_column:
        rule_fields.update(
            key
            for key, binding in links.items()
            if binding.property_id == source.classification_column.property_id
        )
    applicable_alerts = tuple(
        bound
        for entry in config.alerts
        if (entry.sources is None or source.source_id in entry.sources)
        and (bound := bind_scope(entry.rule, source)) is not None
    )
    for rule in applicable_alerts:
        if rule.enabled and rule.scope:
            rule_fields.update(rule.scope.fields)
    column_kinds = {item.id: item.kind for item in config.columns}
    columns = {}
    for item in config.columns:
        if (origin := links.get(item.id)) is None:
            continue
        source_kind = origin.kind or item.legacy_kind or item.kind
        # 실제 선택·상태 타입을 저장하지 않았던 이전 연결은 행의 메타데이터로 판별한다.
        if not (
            item.kind == "text" and source_kind == "status_or_select"
        ) and not accepts_column_type(source.provider, item.kind, source_kind):
            raise ValueError
        columns[item.id] = DisplayColumn(
            item.id,
            item.name,
            source_kind,
            origin.property_id,
            time_only=item.kind == "time",
            full_text=item.kind == "text" and item.id in rule_fields,
            # 업무 상태 역할에 연결한 선택 속성도 자동 배지로 표시한다.
            status_badge=True
            if item.status_badge is None and origin.property_id == source.properties.get("status")
            else item.status_badge,
        )
    previous = inherited_options(config, source.source_id)
    kinds: dict[str, str] = {}
    for column in columns.values():
        if kinds.setdefault(column.property_id, column.kind) != column.kind:
            raise ValueError
    visible = {
        item.id
        for item in config.columns
        if item.visible and item.id not in previous.hidden_columns
    }
    order = tuple(dict.fromkeys((*(previous.column_order or ()), *columns)))
    shown = tuple(columns[key] for key in order if key in visible and key in columns)
    # 연결한 칼럼을 모두 숨긴 선택은 빈 표시다. 이전 알림 전용 칼럼만 있는 소스는 기본 표시를 유지한다.
    custom_display = previous.custom_display or bool(columns.keys() - previous.hidden_columns)
    bindings = {key: (key, col.value_key, column_kinds[key]) for key, col in columns.items()}
    if source.kind == "meeting":
        bindings[EVENT_CLASSIFICATION] = (EVENT_CLASSIFICATION, EVENT_CLASSIFICATION, "text")
    alerts = tuple(
        replace(
            rule,
            scope_bindings=tuple(bindings[key] for key in sorted(rule.scope.fields))
            if rule.scope
            else (),
            column_id=matched.property_id,
            column_kind=matched.kind,
            time_only=matched.time_only,
        )
        for rule in applicable_alerts
        if (matched := columns.get(rule.column_id)) is not None
        and (rule.member is None or matched.kind == "people")
        # 연결되지 않은 비교 칼럼을 빈 값으로 해석하면 적용 범위가 넓어진다.
        and (rule.scope is None or rule.scope.fields <= bindings.keys())
    )
    required = {rule.column_id for rule in applicable_alerts if rule.enabled}
    # 이전 표시 설정에서 숨긴 칼럼도 현재 목록 규칙이 참조하면 수집한다.
    required.update(rule_fields)
    classified = source.classification_column
    if classified:
        # 같은 원본을 표시하는 저장 칼럼은 기존 값 키를 유지한다.
        classified = next(
            (
                col
                for col in columns.values()
                if col.property_id == classified.property_id and col.id == classified.id
            ),
            next(
                (col for col in columns.values() if col.property_id == classified.property_id),
                replace(classified, full_text=True) if classification_rule else classified,
            ),
        )
        required.add(classified.id)
    elif config.classification_column:
        classified = columns.get(config.classification_column)
        required.add(config.classification_column)
    storage = tuple(
        column
        for key, column in columns.items()
        if key not in previous.hidden_columns or key in required or key == config.calendar_date
    )
    return replace(
        source,
        classification_column=classified,
        classification_values=source.classification_values
        if source.classification_column
        else dict(config.classification_values),
        classification_default=source.classification_default
        if source.classification_column
        else config.classification_default,
        stored_columns=storage,
        display_columns=shown if custom_display else None,
        calendar_column=columns.get(config.calendar_date) if config.calendar_date else None,
        calendar_date_override=config.calendar_date is not None,
        alert_rules=alerts,
        failure_rules=tuple(
            entry.rule
            for entry in config.alerts
            if entry.rule.column_id == COLLECTION_STATE
            and entry.rule.enabled
            and (entry.sources is None or source.source_id in entry.sources)
        ),
        event_alert_triggers=frozenset(rule.trigger for rule in config.event_alerts or ()),
    )


def read_display(
    source: SourceMapping, raw: dict[str, object], category: SourceCategory | None
) -> SourceDisplay:
    """저장된 설정만 사용하고 선택 범위 밖의 색·정렬·채널은 거부한다."""
    color, sort, channels = (
        raw.get("color"),
        raw.get("sort", "default"),
        raw.get("notificationChannels"),
    )
    if color is not None and (not isinstance(color, str) or color not in SOURCE_COLORS):
        raise ValueError
    direction = raw.get("sortDirection", "asc")
    if not isinstance(direction, str) or direction not in ("asc", "desc"):
        raise ValueError
    if not isinstance(sort, str) or (
        sort not in SOURCE_SORTS
        and not (
            sort.startswith("column:")
            and category is not None
            and category.configuration is not None
            and sort.removeprefix("column:") in category_bindings(raw)
        )
    ):
        raise ValueError
    calendar_visible = raw.get("calendarVisible", True)
    if not isinstance(calendar_visible, bool):
        raise ValueError
    selected: tuple[str, ...] | None = None
    if channels is not None:
        if not isinstance(channels, list):
            raise ValueError
        selected = tuple(nonblank(item) for item in channels)
        if len(set(selected)) != len(selected) or set(selected) - set(SOURCE_CHANNELS):
            raise ValueError
    events = notification_events(source)
    enabled_types: tuple[str, ...] | None = None
    raw_types = raw.get("notificationTypes")
    if raw_types is not None:
        if not isinstance(raw_types, list):
            raise ValueError
        enabled_types = tuple(nonblank(item) for item in raw_types)
        allowed = {key for key, _ in events}
        if len(set(enabled_types)) != len(enabled_types) or set(enabled_types) - allowed:
            raise ValueError
    config = category.configuration if category is not None else None
    if config is not None:
        old = inherited_options(config, source.source_id)
        color = old.color or config.calendar_color
        calendar_visible = config.calendar_visible and old.calendar_visible is not False
        selected = old.channels if old.channels is not None else config.channels
        enabled_types = old.notification_types
        if config.event_alerts is not None:
            registered = {item.trigger: item.name for item in config.event_alerts}
            events = tuple(
                (kind, registered[trigger])
                for kind, _ in events
                if (trigger := EVENT_ALERT_KINDS.get(kind)) is not None
                and trigger in registered
                and (enabled_types is None or kind in enabled_types)
            )
            enabled_types = tuple(kind for kind, _ in events)
    if config is None or config.event_alerts is None:
        # 이전 문서도 명시적으로 선택한 종류만 노출하고 연결된 속성에서 규칙을 만들지 않는다.
        events = tuple(event for event in events if enabled_types and event[0] in enabled_types)
    return SourceDisplay(
        source.source_id,
        source.kind,
        source.section,
        nonblank(raw["name"]) if "name" in raw else None,
        color,
        sort,
        selected,
        notion_link(raw.get("pageUrl")),
        calendar_visible,
        enabled_types,
        category,
        source.flexible,
        (
            *events,
            *(
                (rule.notification_kind, rule.name)
                for rule in (*source.alert_rules, *source.failure_rules)
                if rule.enabled
            ),
        ),
        sort_direction=direction,
    )


def notification_events(source: SourceMapping) -> tuple[tuple[str, str], ...]:
    """연결한 칼럼과 일정 분류로 가능한 알림을 결정한다."""
    events = source_notifications(source.kind, source.section)
    if not source.flexible:
        return events
    if not source.has_person_scope:
        return tuple(event for event in events if event[0] == "sync_error")
    if source.kind == "task":
        missing = {
            event
            for role, event in (("status", "status"), ("dueDate", "due"))
            if role not in source.properties
        }
        return tuple(event for event in events if event[0].rsplit("_", 1)[-1] not in missing)
    if (
        source.kind == "meeting"
        and source.default_event_type != "meeting"
        and "meeting" not in source.event_types.values()
    ):
        return tuple(event for event in events if event[0] != "meeting")
    return events


def read_event_types(value: object) -> dict[str, EventType]:
    """원본 분류명과 허용한 의미 분류를 엄격하게 연결한다."""
    if not isinstance(value, dict):
        raise ValueError
    rules: dict[str, EventType] = {}
    for key, event_type in value.items():
        category = nonblank(key)
        if category in rules or event_type not in ("meeting", "leave", "other"):
            raise ValueError
        rules[category] = event_type
    return rules


def read_completed_statuses(item: dict[str, object]) -> tuple[str, ...]:
    """조회용 완료 기준만 읽어 수집 매핑과 기존 해시를 유지한다."""
    values = item["completedStatuses"]
    if not isinstance(values, list):
        raise ValueError
    return tuple(nonblank(value) for value in values)
