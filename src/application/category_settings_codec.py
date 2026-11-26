"""카테고리 문서와 상속 옵션을 읽고 저장 형식으로 변환한다."""

from collections.abc import Mapping
from dataclasses import replace
from uuid import UUID

from src.application.alert_settings_codec import (
    alert_delivery_payload,
    alert_rules_payload,
    category_channels,
    parse_alert_delivery,
    parse_alert_rules,
)
from src.application.category_column_types import LEGACY_CATEGORY_TYPES, accepts_column_type
from src.application.errors import COLLECTION_FAILURE_CODES
from src.application.list_settings_codec import main_list_payload, parse_main_list
from src.application.settings_validation import WorkSettingsError, nonblank, optional_strings
from src.domain.category_configuration import (
    CATEGORY_COLUMN_TYPES,
    CategoryAlert,
    CategoryColumn,
    CategoryConfiguration,
    CategorySourceOptions,
    ColumnBinding,
)
from src.domain.category_event_alerts import EVENT_ALERT_TRIGGERS, CategoryEventAlert
from src.domain.display_columns import DISPLAY_COLUMN_TYPES
from src.domain.source_alerts import COLLECTION_STATE, UNBOUND_COLUMN
from src.domain.source_display import SOURCE_COLORS, SourceCategory


def read_category(raw: object) -> SourceCategory | None:
    """공유 카테고리는 이름을 바꿔도 같은 ID를 유지한다."""
    if raw is None:
        return None
    if not isinstance(raw, dict) or set(raw) not in (
        {"id", "name"},
        {"id", "name", "configuration"},
    ):
        raise ValueError
    name = nonblank(raw["name"])
    if len(name) > 80 or any(ord(char) < 32 for char in name):
        raise ValueError
    config = parse_category_configuration(raw["configuration"]) if "configuration" in raw else None
    return SourceCategory(str(UUID(nonblank(raw["id"]))), name, config)


def parse_categories(raw: object) -> tuple[SourceCategory, ...]:
    """독립 목록을 검증하고 이전 등록 목록의 이름 충돌을 보존한다."""
    if not isinstance(raw, list):
        raise ValueError
    categories = tuple(read_category(item) for item in raw)
    if any(item is None for item in categories):
        raise ValueError
    result = tuple(item for item in categories if item is not None)
    if len({item.id for item in result}) != len(result):
        raise ValueError
    return result


def legacy_categories(sources: list[dict[str, object]]) -> tuple[SourceCategory, ...]:
    """이전 문서에 내장된 카테고리를 이름과 ID 그대로 등록한다."""
    categories = tuple(
        dict.fromkeys(
            item
            for source in sources
            if (item := read_category(source.get("category"))) is not None
        )
    )
    if len({item.id for item in categories}) != len(categories):
        raise ValueError
    return categories


def resolve_category(
    raw: dict[str, object], registered: Mapping[str, SourceCategory]
) -> SourceCategory | None:
    """현재 카테고리 이름은 등록 목록에서 읽고 소스는 ID만 참조한다."""
    if "categoryId" in raw:
        if "category" in raw:
            raise ValueError
        return registered[str(UUID(nonblank(raw["categoryId"])))]
    legacy = read_category(raw.get("category"))
    return registered[legacy.id] if legacy else None


def canonical_sources(sources: list[dict[str, object]]) -> tuple[dict[str, object], ...]:
    """이전 소스의 내장 이름을 떼어 내고 기존 ID는 보존한다."""
    normalized = []
    for raw in sources:
        source = dict(raw)
        try:
            if {"category", "categoryId"} <= source.keys():
                raise ValueError
            legacy = read_category(source.pop("category", None))
            if legacy is not None:
                source["categoryId"] = legacy.id
            elif "categoryId" in source:
                source["categoryId"] = str(UUID(nonblank(source["categoryId"])))
        except (ValueError, KeyError, TypeError):
            raise WorkSettingsError("invalid") from None
        normalized.append(source)
    return tuple(normalized)


def category_documents(categories: tuple[SourceCategory, ...]) -> list[dict[str, object]]:
    """저장과 API에 같은 카테고리 목록을 전달한다."""
    result: list[dict[str, object]] = []
    for item in categories:
        raw: dict[str, object] = {"id": item.id, "name": item.name}
        if item.configuration is not None:
            raw["configuration"] = category_configuration_payload(item.configuration)
        result.append(raw)
    return result


def category_color(raw: object) -> str | None:
    """캘린더 기본색은 비어 있을 수 있다."""
    if raw is not None and (not isinstance(raw, str) or raw not in SOURCE_COLORS):
        raise ValueError
    return raw


def parse_category_columns(raw: object) -> tuple[CategoryColumn, ...]:
    """칸의 안정적인 ID와 현재 이름으로 공유 저장 구성을 검증한다."""
    if not isinstance(raw, list):
        raise ValueError
    columns = []
    for item in raw:
        if not isinstance(item, dict) or set(item) - {"legacyKind", "statusBadge"} != {
            "id",
            "name",
            "kind",
            "visible",
        }:
            raise ValueError
        identifier, name, saved_kind = map(nonblank, (item["id"], item["name"], item["kind"]))
        kind = LEGACY_CATEGORY_TYPES.get(saved_kind, saved_kind)
        legacy = item.get("legacyKind", saved_kind if saved_kind != kind else None)
        badge = item.get("statusBadge")
        if badge is not None and not isinstance(badge, bool):
            raise ValueError
        if (
            len(name) > 80
            or kind not in CATEGORY_COLUMN_TYPES
            or type(item["visible"]) is not bool
            or (
                legacy is not None
                and (not isinstance(legacy, str) or not accepts_column_type("notion", kind, legacy))
            )
        ):
            raise ValueError
        columns.append(
            CategoryColumn(str(UUID(identifier)), name, kind, item["visible"], legacy, badge)
        )
    if len({item.id for item in columns}) != len(columns):
        raise ValueError
    return tuple(columns)


def parse_category_alerts(
    raw: object, columns: dict[str, CategoryColumn]
) -> tuple[CategoryAlert, ...]:
    """알림은 현재 카테고리의 호환 타입 칼럼을 참조해야 한다."""
    if not isinstance(raw, list):
        raise ValueError
    rules = []
    for item in raw:
        if not isinstance(item, dict) or set(item) != {"rule", "sources"}:
            raise ValueError
        rule = parse_alert_rules(
            [item["rule"]],
            (*CATEGORY_COLUMN_TYPES, *DISPLAY_COLUMN_TYPES, "collection"),
            scope_columns=columns,
        )[0]
        kind = LEGACY_CATEGORY_TYPES.get(rule.column_kind, rule.column_kind)
        if rule.column_id == COLLECTION_STATE:
            if (
                kind != "collection"
                or rule.item_filter is not None
                or rule.scope is not None
                or (rule.target is not None and rule.target not in COLLECTION_FAILURE_CODES)
            ):
                raise ValueError
        elif rule.column_id == UNBOUND_COLUMN:
            if rule.enabled or kind not in CATEGORY_COLUMN_TYPES:
                raise ValueError
        elif rule.column_id not in columns or columns[rule.column_id].kind != kind:
            raise ValueError
        rules.append(
            CategoryAlert(replace(rule, column_kind=kind), optional_strings(item["sources"]))
        )
    return tuple(rules)


def parse_inherited_options(raw: object) -> tuple[CategorySourceOptions, ...]:
    """이전 소스별 예외는 카테고리 내에서만 읽는다."""
    if not isinstance(raw, list):
        raise ValueError
    options = []
    for item in raw:
        if not isinstance(item, dict) or set(item) - {
            "sourceId",
            "calendarVisible",
            "color",
            "channels",
            "notificationTypes",
            "customDisplay",
            "hiddenColumns",
            "columnOrder",
        }:
            raise ValueError
        visible, custom = item.get("calendarVisible"), item.get("customDisplay")
        if any(value is not None and type(value) is not bool for value in (visible, custom)):
            raise ValueError
        options.append(
            CategorySourceOptions(
                nonblank(item["sourceId"]),
                visible,
                category_color(item.get("color")),
                category_channels(item.get("channels")),
                optional_strings(item.get("notificationTypes")),
                custom,
                optional_strings(item.get("hiddenColumns")) or (),
                optional_strings(item.get("columnOrder")),
            )
        )
    if len({item.source_id for item in options}) != len(options):
        raise ValueError
    return tuple(options)


def parse_category_configuration(raw: object) -> CategoryConfiguration:
    """칼럼 변경이 기존 날짜·알림·목록 규칙의 참조를 깨는지 검사한다."""
    if not isinstance(raw, dict) or set(raw) - {"eventAlerts", "mainList", "classification"} != {
        "columns",
        "calendarVisible",
        "calendarColor",
        "calendarDate",
        "channels",
        "alerts",
        "inherited",
    }:
        raise ValueError
    columns = parse_category_columns(raw["columns"])
    by_id = {item.id: item for item in columns}
    day = raw["calendarDate"]
    if (
        day is not None
        and (not isinstance(day, str) or day not in by_id or by_id[day].kind != "date")
    ) or type(raw["calendarVisible"]) is not bool:
        raise ValueError
    classification_default = None
    classification_column = None
    classification_values: dict[str, str] = {}
    if "classification" in raw:
        classification = raw["classification"]
        if not isinstance(classification, dict) or set(classification) - {"default"} != {
            "column",
            "values",
        }:
            raise ValueError
        if "default" in classification:
            classification_default = nonblank(classification["default"])
        classification_column = nonblank(classification["column"])
        column = by_id.get(classification_column)
        values = classification["values"]
        if column is None or column.kind != "text" or not isinstance(values, dict):
            raise ValueError
        for key, value in values.items():
            name = nonblank(key)
            if name in classification_values:
                raise ValueError
            classification_values[name] = nonblank(value)
    return CategoryConfiguration(
        columns,
        raw["calendarVisible"],
        category_color(raw["calendarColor"]),
        day,
        category_channels(raw["channels"]),
        parse_category_alerts(raw["alerts"], by_id),
        parse_inherited_options(raw["inherited"]),
        parse_category_event_alerts(raw["eventAlerts"]) if "eventAlerts" in raw else None,
        parse_main_list(raw["mainList"], columns) if "mainList" in raw else None,
        classification_column,
        tuple(classification_values.items()),
        classification_default,
    )


def parse_category_event_alerts(raw: object) -> tuple[CategoryEventAlert, ...]:
    """사용자가 등록한 이벤트 조건은 카테고리 안에서 중복할 수 없다."""
    if not isinstance(raw, list):
        raise ValueError
    rules = []
    for item in raw:
        if not isinstance(item, dict) or set(item) - {"delivery"} != {"trigger", "name"}:
            raise ValueError
        trigger, name = map(nonblank, (item["trigger"], item["name"]))
        if (
            trigger not in EVENT_ALERT_TRIGGERS
            or len(name) > 80
            or any(ord(char) < 32 for char in name)
        ):
            raise ValueError
        rules.append(
            CategoryEventAlert(
                trigger,
                name,
                parse_alert_delivery(item["delivery"]) if "delivery" in item else None,
            )
        )
    if len({item.trigger for item in rules}) != len(rules):
        raise ValueError
    return tuple(rules)


def category_configuration_payload(value: CategoryConfiguration) -> dict[str, object]:
    """카테고리 설정 화면과 SQLite가 같은 문서 형식을 사용한다."""
    return {
        "columns": [
            {
                "id": item.id,
                "name": item.name,
                "kind": item.kind,
                "visible": item.visible,
                **({"statusBadge": item.status_badge} if item.status_badge is not None else {}),
                **({"legacyKind": item.legacy_kind} if item.legacy_kind is not None else {}),
            }
            for item in value.columns
        ],
        **(
            {
                "classification": {
                    "column": value.classification_column,
                    "values": dict(value.classification_values),
                    **(
                        {"default": value.classification_default}
                        if value.classification_default
                        else {}
                    ),
                }
            }
            if value.classification_column
            else {}
        ),
        "calendarVisible": value.calendar_visible,
        "calendarColor": value.calendar_color,
        "calendarDate": value.calendar_date,
        **({"mainList": main_list_payload(value.main_list)} if value.main_list is not None else {}),
        "channels": list(value.channels) if value.channels is not None else None,
        "alerts": [
            {
                "rule": alert_rules_payload((entry.rule,))[0],
                "sources": list(entry.sources) if entry.sources is not None else None,
            }
            for entry in value.alerts
        ],
        **(
            {
                "eventAlerts": [
                    {
                        "trigger": item.trigger,
                        "name": item.name,
                        **alert_delivery_payload(item.delivery),
                    }
                    for item in value.event_alerts
                ]
            }
            if value.event_alerts is not None
            else {}
        ),
        "inherited": [
            {
                "sourceId": item.source_id,
                "calendarVisible": item.calendar_visible,
                "color": item.color,
                "channels": list(item.channels) if item.channels is not None else None,
                "notificationTypes": list(item.notification_types)
                if item.notification_types is not None
                else None,
                "customDisplay": item.custom_display,
                "hiddenColumns": list(item.hidden_columns),
                "columnOrder": list(item.column_order) if item.column_order is not None else None,
            }
            for item in value.inherited
        ],
    }


def category_bindings(raw: dict[str, object]) -> dict[str, ColumnBinding]:
    """원본 속성만 있던 이전 연결과 실제 타입이 있는 연결을 읽는다."""
    wanted = raw.get("columnMappings", {})
    if not isinstance(wanted, dict):
        raise ValueError
    result = {}
    for key, value in wanted.items():
        identifier = str(UUID(nonblank(key)))
        if isinstance(value, dict) and set(value) == {"propertyId", "kind"}:
            result[identifier] = ColumnBinding(
                nonblank(value["propertyId"]), nonblank(value["kind"])
            )
        else:
            result[identifier] = ColumnBinding(nonblank(value))
    return result


def column_bindings_payload(links: Mapping[str, ColumnBinding]) -> dict[str, object]:
    """원본 타입을 소스 연결에 저장하고 이전 문자열 형식도 유지한다."""
    return {
        key: {"propertyId": origin.property_id, "kind": origin.kind}
        if origin.kind is not None
        else origin.property_id
        for key, origin in links.items()
    }


def inherited_options(config: CategoryConfiguration, source_id: str) -> CategorySourceOptions:
    """업그레이드 전에 설정한 예외가 있을 때만 기존 표시를 적용한다."""
    return next(
        (item for item in config.inherited if item.source_id == source_id),
        CategorySourceOptions(source_id),
    )
