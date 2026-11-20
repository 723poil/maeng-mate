"""파일·DB와 무관하게 업무 설정 JSON의 구조와 원천 매핑을 검증한다."""

from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from uuid import UUID

from src.application.category_column_types import LEGACY_CATEGORY_TYPES, accepts_column_type
from src.application.errors import COLLECTION_FAILURE_CODES
from src.application.notion_links import notion_link
from src.domain.category_configuration import (
    CATEGORY_COLUMN_TYPES,
    CategoryAlert,
    CategoryColumn,
    CategoryConfiguration,
    CategorySourceOptions,
    ColumnBinding,
)
from src.domain.category_event_alerts import (
    EVENT_ALERT_KINDS,
    EVENT_ALERT_TRIGGERS,
    CategoryEventAlert,
)
from src.domain.daily_work import WorkRules, parse_rules
from src.domain.display_columns import DISPLAY_COLUMN_TYPES, MAX_DISPLAY_COLUMNS, DisplayColumn
from src.domain.main_list import FIELD_KINDS, ListFilter, ListSort, MainListRules
from src.domain.models import ClassificationCondition, ClassificationRule, EventType, SourceMapping
from src.domain.source_alerts import (
    COLLECTION_STATE,
    MAX_SOURCE_ALERT_RULES,
    UNBOUND_COLUMN,
    AlertDelivery,
    AlertSchedule,
    SourceAlertRule,
)
from src.domain.source_display import (
    SOURCE_CHANNELS,
    SOURCE_COLORS,
    SOURCE_SORTS,
    SourceCategory,
    SourceDisplay,
    source_notifications,
)
from src.domain.source_scope import SourceScope, calendar_conditions
from src.domain.task_display import QATaskDisplayPolicy
from src.domain.work_dates import canonical_identity


class WorkSettingsError(ValueError):
    """설정 원문 없이 입력 오류와 저장 충돌을 구분한다."""

    def __init__(self, code: str) -> None:
        """외부 값 대신 고정된 오류 코드만 보관한다."""
        self.code = code if code in {"invalid", "conflict", "missing"} else "storage"
        super().__init__(self.code)


@dataclass(frozen=True)
class Settings:
    """검증된 소스 매핑과 보관 정책 확인 상태다."""

    sources: tuple[SourceMapping, ...]
    retention_confirmed: bool = False
    history_days: int = 90
    log_days: int = 14
    completed_statuses: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    work_rules: WorkRules | None = None
    source_pages: Mapping[str, str] = field(default_factory=dict)
    qa_display_policy: QATaskDisplayPolicy = field(default_factory=QATaskDisplayPolicy)
    display: tuple[SourceDisplay, ...] = ()
    categories: tuple[SourceCategory, ...] = ()

    @property
    def active_sources(self) -> tuple[SourceMapping, ...]:
        """등록 카테고리에 연결된 소스만 수집과 조회에 사용한다."""
        active = {item.id for item in self.display if item.category is not None}
        return tuple(source for source in self.sources if source.source_id in active)


def parse_settings(value: object) -> Settings:
    """외부 JSON을 검증하고 오류에 설정 원문을 포함하지 않는다."""
    try:
        if not isinstance(value, dict) or value.keys() - {
            "version",
            "timezone",
            "sources",
            "categories",
            "categoryAssignmentVersion",
            "classificationVersion",
            "mainListRulesVersion",
            "retention",
            "workRules",
            "qaDisplay",
        }:
            raise ValueError
        # 기존 설정의 버전 비교와 선택 필드의 null 허용 범위를 유지한다.
        rules_version = value.get("mainListRulesVersion", 1)
        if (
            value.get("version") != 1
            or value.get("timezone") != "Asia/Seoul"
            or value.get("categoryAssignmentVersion") not in (None, 1)
            or type(value.get("classificationVersion", 1)) is not int
            or value.get("classificationVersion", 1) not in (1, 2, 3, 4)
            or type(rules_version) is not int
            or rules_version != 1
        ):
            raise ValueError
        raw_sources = source_documents(value)
        sources = tuple(parse_source(item) for item in raw_sources)
        if len({source.source_id for source in sources}) != len(sources):
            raise ValueError
        completed = {
            source.source_id: read_completed_statuses(raw)
            for source, raw in zip(sources, raw_sources, strict=True)
            if source.kind == "task" and "completedStatuses" in raw
        }
        work_rules = parse_rules(value.get("workRules"))
        qa_display = read_qa_display(value.get("qaDisplay"))
        source_pages = {
            source.source_id: nonblank(raw["pageUrl"])
            for source, raw in zip(sources, raw_sources, strict=True)
            if "pageUrl" in raw
        }
        categories = (
            parse_categories(value["categories"])
            if value.get("categories") is not None
            else legacy_categories(raw_sources)
        )
        by_id = {category.id: category for category in categories}
        rule_ids = [rule.id for source in sources for rule in source.alert_rules]
        rule_ids.extend(
            entry.rule.id
            for category in categories
            if category.configuration
            for entry in category.configuration.alerts
        )
        if len(set(rule_ids)) != len(rule_ids):
            raise ValueError
        assignments = tuple(resolve_category(raw, by_id) for raw in raw_sources)
        sources = tuple(
            resolve_category_mapping(source, raw, category)
            for source, raw, category in zip(sources, raw_sources, assignments, strict=True)
        )
        display = tuple(
            read_display(source, raw, category)
            for source, raw, category in zip(sources, raw_sources, assignments, strict=True)
        )
        settings = Settings(
            sources,
            completed_statuses=completed,
            work_rules=work_rules,
            source_pages=source_pages,
            qa_display_policy=qa_display,
            display=display,
            categories=categories,
        )
        retention = value.get("retention")
        if retention is None:
            return settings
        if not isinstance(retention, dict) or set(retention) != {
            "historyDays",
            "logDays",
            "confirmed",
        }:
            raise ValueError
        days, logs, confirmed = (
            retention["historyDays"],
            retention["logDays"],
            retention["confirmed"],
        )
        if type(days) is not int or type(logs) is not int or type(confirmed) is not bool:
            raise ValueError
        if not 1 <= days <= 3650 or not 1 <= logs <= 3650:
            raise ValueError
        return replace(settings, retention_confirmed=confirmed, history_days=days, log_days=logs)
    except (ValueError, TypeError, KeyError):
        raise WorkSettingsError("invalid") from None


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
        if not flexible or kind != "meeting":
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
    if "roundTitleProperty" in item:
        allowed.add("roundTitleProperty")
        if kind != "task" or (section != "qa" and not flexible):
            raise ValueError
    allowed.update(
        key
        for key in (
            "color",
            "sort",
            "notificationChannels",
            "notificationTypes",
            "calendarVisible",
            "category",
            "categoryId",
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
    source_id = nonblank(item["dataSourceId"])
    try:
        source_id = str(UUID(source_id))
    except ValueError:
        pass  # 가상 자료와 호환하되 실제 UUID는 정규 표기로 중복 검사한다.
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
    required_keys = {
        "task": {"title", "assignees", "status"},
        "meeting": {"title", "participants", "period"},
        "leave": {"people", "period", "approval"},
    }[kind]
    optional_keys = (
        {"dueDate"} if kind == "task" else {"category", "cancelled"} if kind == "meeting" else set()
    )
    if flexible and kind in {"task", "meeting"}:
        # 표를 읽는 데 필요한 역할만 필수로 둔다.
        optional_roles = {"assignees", "status"} if kind == "task" else {"participants"}
        optional_keys |= optional_roles
        required_keys -= optional_roles
    if not required_keys <= properties.keys() or properties.keys() - required_keys - optional_keys:
        raise ValueError
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
        nonblank(item["roundTitleProperty"]) if "roundTitleProperty" in item else None,
        provider,
        flexible,
        "other" if event_default == "other" else "leave" if event_default == "leave" else "meeting",
        parse_display_columns(item["displayColumns"], status_property=properties.get("status"))
        if "displayColumns" in item
        else None,
        parse_alert_rules(item["alertRules"]) if "alertRules" in item else (),
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


def parse_alert_delivery(value: object) -> AlertDelivery:
    """명시적인 빈 채널을 보존하고 알림 발신에 사용할 값만 허용한다."""
    if not isinstance(value, dict) or set(value) - {"bodyToken"} != {"message", "channels"}:
        raise ValueError
    channels = category_channels(value["channels"])
    message = value["message"]
    body_token = value.get("bodyToken")
    if body_token is not None and not isinstance(body_token, str):
        raise ValueError
    if message is not None and not isinstance(message, str):
        raise ValueError
    return AlertDelivery(message.strip() if message is not None else None, channels, body_token)


def alert_delivery_payload(delivery: AlertDelivery | None) -> dict[str, object]:
    """기존 문서는 전달 설정을 생략해 원래 본문과 채널 선택을 유지한다."""
    if delivery is None:
        return {}
    channels = list(delivery.channels) if delivery.channels is not None else None
    return {
        "delivery": {
            "message": delivery.message,
            "channels": channels,
            **({"bodyToken": delivery.body_token} if delivery.body_token is not None else {}),
        }
    }


def parse_alert_schedule(raw: object, kind: str, target: object) -> AlertSchedule:
    """날짜·시간 칼럼만 예약 대상으로 받고 값 변경 조건과 혼용하지 않는다."""
    if kind not in {"date", "time", "created_time", "last_edited_time"} or target is not None:
        raise ValueError("예약 규칙에는 날짜 또는 시간 칼럼을 선택하세요.")
    if not isinstance(raw, dict) or set(raw) != {"mode", "value"}:
        raise ValueError("알림 예약 조건을 확인하세요.")
    mode, value = raw["mode"], raw["value"]
    if mode not in ("same_day", "previous_day", "before_start") or not isinstance(value, str):
        raise ValueError("알림 예약 입력을 확인하세요.")
    return AlertSchedule(mode, value)


def parse_alert_scope(
    raw: object, columns: Mapping[str, CategoryColumn] | None
) -> MainListRules | None:
    """항목 조건은 카테고리의 저장 칼럼만 사용하며 빈 조건은 기본 범위로 취급한다."""
    if (
        columns is None
        or not isinstance(raw, dict)
        or set(raw) - {"exclusions"} != {"match", "filters"}
    ):
        raise ValueError
    match = raw["match"]
    if not isinstance(match, str):
        raise ValueError
    rules = MainListRules(
        match,
        parse_list_filters(raw["filters"]),
        exclusions=parse_list_filters(raw.get("exclusions", [])),
    )
    rules.validate({key: col.kind for key, col in columns.items()})
    return rules if rules.fields else None


def parse_source_scopes(raw: object) -> tuple[SourceScope, ...]:
    """표별 조건과 원본 칼럼 참조를 검증하고 표 중복을 막는다."""
    if not isinstance(raw, list):
        raise ValueError
    result = []
    for item in raw:
        if not isinstance(item, dict) or set(item) - {"exclusions"} != {
            "sourceId",
            "match",
            "filters",
        }:
            raise ValueError
        rules = MainListRules(
            nonblank(item["match"]),
            parse_list_filters(item["filters"], source_options=True),
            exclusions=parse_list_filters(item.get("exclusions", []), source_options=True),
        )
        scope = SourceScope(canonical_identity(nonblank(item["sourceId"])), rules)
        scope.validate()
        result.append(scope)
    if len({item.source_id for item in result}) != len(result):
        raise ValueError
    return tuple(result)


def parse_alert_rules(
    raw: object,
    kinds: tuple[str, ...] = DISPLAY_COLUMN_TYPES,
    *,
    scope_columns: Mapping[str, CategoryColumn] | None = None,
) -> tuple[SourceAlertRule, ...]:
    """원본 속성과 규칙의 이름·조건·사용 여부를 확인한다."""
    if not isinstance(raw, list) or len(raw) > MAX_SOURCE_ALERT_RULES:
        raise ValueError
    rules = []
    for item in raw:
        if not isinstance(item, dict) or set(item) - {
            "delivery",
            "schedule",
            "member",
            "itemFilter",
            "sourceScopes",
            "scope",
            "previousKind",
        } != {
            "id",
            "name",
            "columnId",
            "columnKind",
            "target",
            "enabled",
        }:
            raise ValueError
        name, column, kind = map(nonblank, (item["name"], item["columnId"], item["columnKind"]))
        target, enabled = item["target"], item["enabled"]
        if (
            len(name) > 80
            or any(ord(char) < 32 for char in name)
            or kind not in kinds
            or (target is not None and (not isinstance(target, str) or len(target) > 240))
            or type(enabled) is not bool
        ):
            raise ValueError
        prior = item.get("previousKind")
        if prior is not None and (not isinstance(prior, str) or prior not in EVENT_ALERT_KINDS):
            raise ValueError
        member = item.get("member")
        if member is not None:
            if (
                not isinstance(member, str)
                or kind not in {"text", "people"}
                or target is not None
                or "schedule" in item
            ):
                raise ValueError
            if member != "self":
                member = str(UUID(member))
        item_filter = item.get("itemFilter")
        if item_filter not in (None, "meeting", "upcoming"):
            raise ValueError
        if "sourceScopes" in item and (
            scope_columns is None or item_filter is not None or column == COLLECTION_STATE
        ):
            raise ValueError
        value = None if target is None else " ".join(target.split())
        rules.append(
            SourceAlertRule(
                str(UUID(nonblank(item["id"]))),
                name,
                column,
                kind,
                value,
                enabled,
                delivery=parse_alert_delivery(item["delivery"]) if "delivery" in item else None,
                member=member,
                item_filter=item_filter,
                source_scopes=parse_source_scopes(item["sourceScopes"])
                if "sourceScopes" in item
                else None,
                scope=parse_alert_scope(item["scope"], scope_columns) if "scope" in item else None,
                previous_kind=prior,
                schedule=parse_alert_schedule(item["schedule"], kind, target)
                if "schedule" in item
                else None,
            )
        )
    if len({rule.id for rule in rules}) != len(rules):
        raise ValueError
    return tuple(rules)


def alert_rules_payload(rules: tuple[SourceAlertRule, ...]) -> list[dict[str, object]]:
    """설정 화면의 이름과 조건을 JSON으로 저장한다."""
    return [
        {
            "id": rule.id,
            "name": rule.name,
            "columnId": rule.column_id,
            "columnKind": rule.column_kind,
            "target": rule.target,
            "enabled": rule.enabled,
            **alert_delivery_payload(rule.delivery),
            **({"previousKind": rule.previous_kind} if rule.previous_kind else {}),
            **({"member": rule.member} if rule.member is not None else {}),
            **({"scope": filter_conditions_payload(rule.scope)} if rule.scope else {}),
            **(
                {
                    "sourceScopes": [
                        {"sourceId": sc.source_id, **filter_conditions_payload(sc.conditions)}
                        for sc in rule.source_scopes
                    ]
                }
                if rule.source_scopes is not None
                else {}
            ),
            **({"itemFilter": rule.item_filter} if rule.item_filter is not None else {}),
            **(
                {"schedule": {"mode": rule.schedule.mode, "value": rule.schedule.value}}
                if rule.schedule
                else {}
            ),
        }
        for rule in rules
    ]


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


def optional_strings(raw: object) -> tuple[str, ...] | None:
    """미지정과 빈 선택을 구분하는 목록이다."""
    if raw is None:
        return None
    if not isinstance(raw, list):
        raise ValueError
    items = tuple(nonblank(item) for item in raw)
    if len(items) != len(set(items)):
        raise ValueError
    return items


def category_channels(raw: object) -> tuple[str, ...] | None:
    """카테고리에서 실제 사용할 수 있는 전달 채널을 검증한다."""
    result = optional_strings(raw)
    if result is not None and set(result) - set(SOURCE_CHANNELS):
        raise ValueError
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


def parse_list_filters(raw: object, *, source_options: bool = False) -> tuple[ListFilter, ...]:
    """같은 형식으로 저장한 포함·제외 조건의 입력을 검사한다."""
    if not isinstance(raw, list):
        raise ValueError
    conditions: list[ListFilter] = []
    optional = {"optionId"} if source_options else set()
    for item in raw:
        if (
            not isinstance(item, dict)
            or set(item) - optional != {"field", "operator", "value"}
            or not isinstance(item["value"], str)
        ):
            raise ValueError
        conditions.append(
            ListFilter(
                nonblank(item["field"]),
                nonblank(item["operator"]),
                item["value"],
                nonblank(item["optionId"]) if "optionId" in item else None,
            )
        )
    return tuple(conditions)


def parse_main_list(raw: object, columns: tuple[CategoryColumn, ...]) -> MainListRules:
    """목록 규칙의 JSON 구조와 카테고리 안에서 사용할 수 있는 조건을 검증한다."""
    # 이전 체크박스의 해석은 설정 이전에서만 처리한다.
    if not isinstance(raw, dict) or set(raw) - {"useDefaultVisibility", "exclusions"} != {
        "match",
        "filters",
        "sorts",
    }:
        raise ValueError
    match, sorts = raw["match"], raw["sorts"]
    if (
        not isinstance(match, str)
        or ("useDefaultVisibility" in raw and type(raw["useDefaultVisibility"]) is not bool)
        or not isinstance(sorts, list)
    ):
        raise ValueError
    conditions = parse_list_filters(raw["filters"])
    exclusions = parse_list_filters(raw.get("exclusions", []))
    order = []
    for item in sorts:
        if (
            not isinstance(item, dict)
            or set(item) - {"values"} != {"field", "descending"}
            or type(item["descending"]) is not bool
        ):
            raise ValueError
        values = item.get("values", [])
        if not isinstance(values, list):
            raise ValueError
        order.append(
            ListSort(
                nonblank(item["field"]), item["descending"], tuple(nonblank(v) for v in values)
            )
        )
    rules = MainListRules(match, conditions, tuple(order), exclusions)
    # 표시를 끈 칼럼도 원본 값은 비교할 수 있으므로 등록된 칼럼 전체를 확인한다.
    rules.validate({**FIELD_KINDS, **{column.id: column.kind for column in columns}})
    return rules


def filter_conditions_payload(rules: MainListRules) -> dict[str, object]:
    """목록과 알림의 포함·제외 조건을 같은 형식으로 기록한다."""

    def condition(item: ListFilter) -> dict[str, str]:
        """원본 옵션 ID가 있는 조건만 이름 변경을 추적하도록 저장한다."""
        return {
            "field": item.field,
            "operator": item.operator,
            "value": item.value,
            **({"optionId": item.option_id} if item.option_id is not None else {}),
        }

    payload: dict[str, object] = {
        "match": rules.match,
        "filters": [condition(item) for item in rules.filters],
    }
    if rules.exclusions:
        payload["exclusions"] = [condition(item) for item in rules.exclusions]
    return payload


def main_list_payload(rules: MainListRules) -> dict[str, object]:
    """표시 조건에 목록 전용 정렬 우선순위를 더한다."""
    return {
        **filter_conditions_payload(rules),
        "sorts": [
            {
                "field": item.field,
                "descending": item.descending,
                **({"values": list(item.values)} if item.values else {}),
            }
            for item in rules.sorts
        ],
    }


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
    alerts = tuple(
        replace(
            rule,
            scope_bindings=tuple(
                (key, columns[key].value_key, column_kinds[key])
                for key in sorted(rule.scope.fields)
            )
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
        and (rule.scope is None or rule.scope.fields <= columns.keys())
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
    if not isinstance(sort, str) or sort not in SOURCE_SORTS:
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


def nonblank(value: object) -> str:
    """외부 설정의 문자열을 확인한 뒤 타입을 좁힌다."""
    if not isinstance(value, str) or not value.strip():
        raise ValueError
    return value.strip()


def read_completed_statuses(item: dict[str, object]) -> tuple[str, ...]:
    """조회용 완료 기준만 읽어 수집 매핑과 기존 해시를 유지한다."""
    values = item["completedStatuses"]
    if not isinstance(values, list):
        raise ValueError
    return tuple(nonblank(value) for value in values)


def read_qa_display(value: object) -> QATaskDisplayPolicy:
    """QA 노출 기간과 종료 항목을 숨길 차시를 개인 설정으로 분리한다."""
    if value is None:
        return QATaskDisplayPolicy()
    required = {"completedRetentionDays", "hiddenCompletedRounds"}
    if (
        not isinstance(value, dict)
        or not required <= value.keys()
        or value.keys() - required - {"hiddenStatuses"}
    ):
        raise ValueError
    days, rounds = value["completedRetentionDays"], value["hiddenCompletedRounds"]
    if type(days) is not int or not 0 <= days <= 3650 or not isinstance(rounds, list):
        raise ValueError
    hidden = value.get("hiddenStatuses", [])
    if not isinstance(hidden, list):
        raise ValueError
    return QATaskDisplayPolicy(
        days, tuple(nonblank(name) for name in rounds), tuple(nonblank(status) for status in hidden)
    )


@dataclass(frozen=True)
class WorkSettingsDocument:
    """원문을 repr에서 제외한 버전 있는 JSON 설정이다."""

    revision: int
    payload: dict[str, object] = field(repr=False)


def empty_work_settings() -> dict[str, object]:
    """새 설치에서만 사용하는 미설정 문서를 매번 새 값으로 만든다."""
    return {
        "version": 1,
        "timezone": "Asia/Seoul",
        "sources": [],
        "mainListRulesVersion": 1,
        "classificationVersion": 4,
    }


def is_initial_document(document: WorkSettingsDocument) -> bool:
    """새 설치의 빈 문서만 최초 가져오기 대상으로 구분한다."""
    # 표시 규칙 버전만 없던 초기 문서도 한 번의 파일 가져오기를 허용한다.
    normalized = {"mainListRulesVersion": 1, "classificationVersion": 4, **document.payload}
    if normalized["classificationVersion"] in (1, 2, 3):
        normalized["classificationVersion"] = 4
    return document.revision == 1 and normalized == empty_work_settings()


def source_documents(payload: dict[str, object]) -> list[dict[str, object]]:
    """JSON 소스 목록의 키를 검사하고 수정 가능한 사본을 반환한다."""
    raw = payload.get("sources")
    if not isinstance(raw, list):
        raise WorkSettingsError("invalid")
    result: list[dict[str, object]] = []
    for source in raw:
        if not isinstance(source, dict) or any(not isinstance(key, str) for key in source):
            raise WorkSettingsError("invalid")
        result.append({key: value for key, value in source.items()})
    return result
