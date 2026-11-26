"""등록된 옛 이벤트를 실제 소스 연결과 구독 상태에 따라 편집 가능한 규칙으로 변환한다."""

from dataclasses import replace
from urllib.parse import unquote
from uuid import UUID, uuid5

from src.application.category_settings_codec import (
    category_bindings,
    category_documents,
    inherited_options,
)
from src.application.source_settings_codec import notification_events
from src.application.work_settings import parse_settings, source_documents
from src.domain.category_configuration import CategoryAlert, CategoryConfiguration
from src.domain.category_event_alerts import EVENT_ALERT_TRIGGERS, CategoryEventAlert
from src.domain.models import SourceMapping
from src.domain.preferences import Preferences
from src.domain.source_alerts import (
    COLLECTION_STATE,
    UNBOUND_COLUMN,
    AlertSchedule,
    SourceAlertRule,
)
from src.domain.source_display import SourceCategory
from src.domain.source_scope import SourceScope, calendar_conditions

# 이전 형식의 역할은 이전 때만 해석한다. 새 문서의 발송 조건에는 실제 저장 칼럼 ID를 쓴다.
_OLD_FIELDS = {
    "calendar": ("participants", "text"),
    "assigned": ("assignees", "text"),
    "status": ("status", "text"),
    "due": ("dueDate", "date"),
    "meeting": ("period", "date"),
    "sync_error": ("", "collection"),
}


def convert_event(
    category: SourceCategory,
    config: CategoryConfiguration,
    event: CategoryEventAlert,
    members: list[tuple[SourceMapping, dict[str, object]]],
    preferences: Preferences,
) -> list[CategoryAlert]:
    """소스별 역할 연결이나 켜짐 상태가 다를 때만 적용 대상을 나눈다."""
    role, kind = _OLD_FIELDS[event.trigger]
    groups: dict[tuple[str, str, bool], list[str]] = {}
    for source, raw in members:
        if event.trigger in {"calendar", "meeting"} and source.kind != "meeting":
            continue
        if event.trigger in {"assigned", "status", "due"} and source.kind != "task":
            continue
        old_kind = next(
            key
            for key in EVENT_ALERT_TRIGGERS[event.trigger].kinds
            if not key.startswith(("task_", "qa_"))
            or key.startswith("qa_" if source.section == "qa" else "task_")
        )
        links = category_bindings(raw)
        origin = source.properties.get(role)
        column = next(
            (
                c.id
                for c in config.columns
                if (c.kind == kind or (event.trigger == "meeting" and c.kind == "time"))
                and c.id in links
                and origin is not None
                and (
                    links[c.id].property_id == origin
                    if source.round_title_property
                    else unquote(links[c.id].property_id) == unquote(origin)
                )
            ),
            UNBOUND_COLUMN,
        )
        if event.trigger == "sync_error":
            column = COLLECTION_STATE
        types = inherited_options(config, source.source_id).notification_types
        enabled = (
            column != UNBOUND_COLUMN
            and old_kind in dict(notification_events(source))
            and (types is None or old_kind in types)
            and preferences.notification_enabled(old_kind, category.key)
        )
        groups.setdefault((column, old_kind, enabled), []).append(source.source_id)
    if not groups:
        key = EVENT_ALERT_TRIGGERS[event.trigger].kinds[0]
        groups[
            (
                COLLECTION_STATE if not role else UNBOUND_COLUMN,
                key,
                not role and preferences.notification_enabled(key, category.key),
            )
        ] = []
    converted = []
    for (column, old_kind, enabled), ids in groups.items():
        identifier = str(
            uuid5(UUID(category.id), f"alert:{event.trigger}:{column}:{old_kind}:{enabled}")
        )
        rule = SourceAlertRule(
            identifier,
            event.name,
            column,
            next((c.kind for c in config.columns if c.id == column), kind),
            enabled=enabled,
            delivery=event.delivery,
            schedule=AlertSchedule("before_start", "10") if event.trigger == "meeting" else None,
            member="self" if event.trigger in {"calendar", "assigned"} else None,
            item_filter="meeting"
            if event.trigger == "meeting"
            else "upcoming"
            if event.trigger == "calendar"
            else None,
            previous_kind=old_kind,
        )
        converted.append(
            CategoryAlert(rule, tuple(ids) if ids and len(ids) != len(members) else None)
        )
    return converted


def upgrade_alert_schema(
    payload: dict[str, object], preferences: Preferences
) -> dict[str, object] | None:
    """이미 저장된 규칙만 변환하고 빈 목록과 이미 변환된 문서는 유지한다."""
    settings = parse_settings(payload)
    if not any(
        cat.configuration
        and (
            cat.configuration.event_alerts
            or any(a.rule.item_filter for a in cat.configuration.alerts)
        )
        for cat in settings.categories
    ):
        return None
    members_by_category: dict[str, list[tuple[SourceMapping, dict[str, object]]]] = {}
    for source, style, raw in zip(
        settings.sources, settings.display, source_documents(payload), strict=True
    ):
        if style.category is not None:
            members_by_category.setdefault(style.category.id, []).append((source, raw))
    categories = []
    for category in settings.categories:
        config = category.configuration
        if config is not None:
            members = members_by_category.get(category.id, [])
            alerts = list(config.alerts)
            for event in config.event_alerts or ():
                alerts.extend(convert_event(category, config, event, members, preferences))
            for index, alert in enumerate(alerts):
                key = alert.rule.item_filter
                if key is None:
                    continue
                # 유형값·날짜 속성은 연결된 표마다 다르다. 원본이 없는 규칙은 발송하지 않는다.
                scopes = tuple(
                    SourceScope(source.source_id, conditions)
                    for source, _ in members
                    if (alert.sources is None or source.source_id in alert.sources)
                    and (conditions := calendar_conditions(source, key)) is not None
                )
                alerts[index] = replace(
                    alert,
                    sources=None,
                    rule=replace(
                        alert.rule,
                        item_filter=None,
                        source_scopes=scopes,
                        enabled=alert.rule.enabled and bool(scopes),
                    ),
                )
            category = replace(
                category, configuration=replace(config, alerts=tuple(alerts), event_alerts=())
            )
        categories.append(category)
    return {**payload, "categories": category_documents(tuple(categories))}
