"""현재 본인·설정 소스로만 알림 큐를 제한한다."""

from src.application.work_settings import Settings
from src.bootstrap.environment import AppEnvironment
from src.bootstrap.notification_channels import CHANNELS
from src.domain.work_dates import canonical_identity
from src.infrastructure.sqlite.connection import Database
from src.infrastructure.sqlite.field_alerts import field_alert_scopes
from src.infrastructure.sqlite.notification_keys import (
    failure_rule_scope,
    meeting_scope,
    notification_key,
    sync_error_scope,
    task_scope,
)
from src.infrastructure.sqlite.notifications import SQLiteNotifications
from src.infrastructure.sqlite.rule_delivery import category_rules
from src.infrastructure.sqlite.serialization import source_fingerprint


def build_notifications(
    environment: AppEnvironment, settings: Settings | None
) -> tuple[SQLiteNotifications | None, dict[str, str]]:
    """예약 범위와 설정 저장에 사용할 소스 그룹을 같은 매핑에서 한 번만 만든다."""
    if environment.database is None:
        return None, {}
    user = canonical_identity(environment.notion_user_id) if environment.notion_user_id else None
    groups: dict[str, str] = {}
    source_keys: list[str] = []
    qa_scopes: list[str] = []
    source_channels: dict[str, tuple[str, ...]] = {}
    source_types: dict[str, tuple[str, ...]] = {}
    source_titles: dict[str, dict[str, str]] = {}
    if settings is not None:
        displays = {
            item.id: (item, item.category.key) for item in settings.display if item.category
        }
        for source in settings.active_sources:
            display, group = displays[source.source_id]
            digest = source_fingerprint(source)
            keys = [sync_error_scope(source.source_id, digest)]
            if user is not None and source.kind == "meeting":
                keys.extend(
                    (
                        notification_key(f"{source.source_id}:{user}"),
                        meeting_scope(source.source_id, user, digest),
                    )
                )
            elif user is not None and source.kind == "task":
                task = task_scope(source.source_id, user, digest)
                keys.append(task)
                if source.section == "qa":
                    qa_scopes.append(task)
            fields = field_alert_scopes(source, user)
            fields.update(
                {
                    failure_rule_scope(source.source_id, digest, rule.id, rule.target): rule
                    for rule in source.failure_rules
                    if rule.enabled
                }
            )
            keys.extend(fields)
            source_keys.extend(keys)
            for key in keys:
                groups[key] = group
                source_titles[key] = dict(display.events)
                if display.channels is not None:
                    source_channels[key] = display.channels
                if display.notification_types is not None:
                    source_types[key] = (
                        *display.notification_types,
                        *(
                            r.previous_kind
                            for r in (*source.alert_rules, *source.failure_rules)
                            if r.enabled and r.previous_kind
                        ),
                    )
            # 칼럼별 알림은 소스의 일반 구독 대신 해당 규칙의 종류를 사용한다.
            for scope, rule in fields.items():
                source_types[scope] = (rule.notification_kind,)
    store = SQLiteNotifications(
        Database(environment.database),
        source_keys=tuple(source_keys),
        user_id=environment.notion_user_id,
        channels=tuple(channel.key for channel in CHANNELS),
        source_channels=source_channels,
        source_types=source_types,
        source_titles=source_titles,
        notification_groups=groups,
        rules_by_group={item.key: category_rules(item) for item in settings.categories}
        if settings
        else {},
        qa_scopes=tuple(qa_scopes),
    )
    return store, groups
