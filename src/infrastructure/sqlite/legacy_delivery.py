"""이전 종류로 예약된 알림을 해당 표에서 이전한 규칙 하나에만 연결한다."""

import sqlite3
from dataclasses import dataclass

from src.application.work_settings import Settings
from src.domain.field_alerts import field_alert_scopes
from src.domain.source_alerts import SourceAlertRule
from src.domain.source_display import SOURCE_CHANNELS
from src.domain.source_fingerprints import source_fingerprint
from src.domain.work_dates import canonical_identity
from src.infrastructure.sqlite.notification_keys import (
    failure_rule_scope,
    meeting_scope,
    notification_key,
    sync_error_scope,
    task_scope,
)
from src.infrastructure.sqlite.rule_delivery import RuleRoute, category_rules


@dataclass(frozen=True)
class LegacyBinding:
    """옛 큐의 표·종류를 현재 조건의 실행 키 및 발송 문구와 연결한다."""

    scope: str
    kind: str
    route: RuleRoute


type LegacyBindings = dict[str, dict[str, LegacyBinding]]


def legacy_bindings(settings: Settings | None, user_id: str | None) -> LegacyBindings:
    """같은 카테고리의 다른 표가 같은 옛 종류를 사용해도 각각의 규칙을 보존한다."""
    if settings is None:
        return {}
    routes = {item.id: category_rules(item) for item in settings.categories}
    result: LegacyBindings = {}
    user = canonical_identity(user_id) if user_id else None
    for source, display in zip(settings.sources, settings.display, strict=True):
        category = display.category
        if category is None or not any(
            rule.enabled and rule.previous_kind
            for rule in (*source.alert_rules, *source.failure_rules)
        ):
            continue
        digest = source_fingerprint(source)
        old_keys = {"sync_error": sync_error_scope(source.source_id, digest)}
        if user and source.kind == "meeting":
            old_keys["calendar"] = notification_key(f"{source.source_id}:{user}")
            old_keys["meeting"] = meeting_scope(source.source_id, user, digest)
        elif user and source.kind == "task":
            prefix = "qa" if source.section == "qa" else "task"
            key = task_scope(source.source_id, user, digest)
            old_keys.update({f"{prefix}_{event}": key for event in ("assigned", "status", "due")})
        scopes: dict[str, SourceAlertRule] = field_alert_scopes(source, user)
        scopes.update(
            {
                failure_rule_scope(source.source_id, digest, rule.id, rule.target): rule
                for rule in source.failure_rules
                if rule.enabled
            }
        )
        for scope, rule in scopes.items():
            old = rule.previous_kind
            if (
                old is not None
                and old in old_keys
                and rule.notification_kind in routes[category.id]
            ):
                result.setdefault(old_keys[old], {})[old] = LegacyBinding(
                    scope, rule.notification_kind, routes[category.id][rule.notification_kind]
                )
    return result


def retire_legacy_deliveries(
    connection: sqlite3.Connection, before: LegacyBindings, after: LegacyBindings
) -> None:
    """옛 예약은 새 조건을 증명하지 못하므로 재수집 전에도 해당 표의 실행권을 무효화한다."""
    for key, bindings in before.items():
        for kind, old in bindings.items():
            current = after.get(key, {}).get(kind)
            if current == old:
                continue
            if current is None or current.scope != old.scope or current.kind != old.kind:
                # 한 업무 변경에 여러 종류가 묶이면 그 변경 건의 기존 본문 전체를 폐기한다.
                connection.execute(
                    """UPDATE notification_jobs SET state='obsolete'
                    WHERE source_key=? AND state IN ('pending','delivering') AND
                    (kind=? OR (kind='task' AND EXISTS
                    (SELECT 1 FROM task_changes c, json_each(c.kinds) k
                     WHERE c.id=notification_jobs.task_change_id AND k.value=?)))""",
                    (key, kind, kind.rsplit("_", 1)[-1]),
                )
            elif current.route != old.route:
                for channel in SOURCE_CHANNELS:
                    if old.route.channels is not None and channel not in old.route.channels:
                        continue
                    if (
                        current.route.title == old.route.title
                        and current.route.message == old.route.message
                        and current.route.body_token == old.route.body_token
                        and (current.route.channels is None or channel in current.route.channels)
                    ):
                        continue
                    # 발송 시각 전인 논리 예약은 현재 문구로 준비하고 이미 준비한 채널만 취소한다.
                    connection.execute(
                        """UPDATE notification_deliveries SET revoked=1,
                        state=CASE WHEN state='pending' THEN 'cancelled' ELSE state END
                        WHERE channel=? AND state IN ('pending','delivering') AND revoked=0
                        AND EXISTS(SELECT 1 FROM json_each(kinds) WHERE value IN (?,?))
                        AND job_id IN (SELECT id FROM notification_jobs WHERE source_key=?)""",
                        (channel, kind, old.kind, key),
                    )
