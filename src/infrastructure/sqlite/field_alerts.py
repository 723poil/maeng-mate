"""성공한 수집 안에서 속성 변경을 감지하고 최신 값에만 알림을 허용한다."""

import json
import sqlite3
from collections.abc import Sequence
from datetime import datetime

from src.application.models import SyncRun
from src.application.notion_links import notion_link
from src.application.work_settings import Settings, parse_settings
from src.domain.models import CalendarEvent, Leave, SourceMapping, Task, WorkItem
from src.domain.source_alerts import SourceAlertRule
from src.domain.work_dates import canonical_identity
from src.infrastructure.sqlite.notification_keys import notification_key
from src.infrastructure.sqlite.serialization import fingerprint, source_fingerprint


def field_alert_scopes(source: SourceMapping, user: str | None) -> dict[str, SourceAlertRule]:
    """규칙, 원본 매핑, 본인 범위를 한 발송 범위로 묶는다."""
    if source.has_person_scope and user is None:
        return {}
    identity = canonical_identity(user) if source.has_person_scope and user else None
    mapping = source_fingerprint(source)
    return {
        fingerprint(
            (
                source.source_id,
                mapping,
                identity,
                rule.id,
                rule.column_id,
                rule.comparison_kind,
                rule.target,
            )
        ): rule
        for rule in source.alert_rules
        if rule.enabled
    }


def saved_rule_bindings(
    settings: Settings,
) -> dict[tuple[str, str], tuple[str, str, str | None, str]]:
    """규칙 이름을 제외한 실제 조건을 저장값과 실행값 사이에서 비교한다."""
    return {
        (source.source_id, rule.notification_kind): (
            rule.column_id,
            rule.comparison_kind,
            rule.target,
            source_fingerprint(source),
        )
        for source in settings.active_sources
        for rule in source.alert_rules
        if rule.enabled
    }


def retire_changed_rules(connection: sqlite3.Connection, before: Settings, after: Settings) -> None:
    """규칙을 끄거나 바꾸면 이전 예약과 기준을 설정 저장과 함께 무효화한다."""
    stored = saved_rule_bindings(after)
    removed = [
        key for key, binding in saved_rule_bindings(before).items() if stored.get(key) != binding
    ]
    connection.executemany(
        """UPDATE notification_jobs SET state='obsolete'
        WHERE kind='source_change' AND state IN ('pending','delivering')
        AND id IN (SELECT job_id FROM source_alert_events WHERE source_id=? AND kind_key=?)""",
        removed,
    )
    connection.executemany(
        "DELETE FROM source_alert_baselines WHERE source_id=? AND kind_key=?", removed
    )


def currently_saved_scopes(
    connection: sqlite3.Connection, source: SourceMapping, user: str | None
) -> dict[str, SourceAlertRule]:
    """설정 저장 전에 시작한 수집도 이미 해제된 조건으로 알림을 만들지 않는다."""
    running = field_alert_scopes(source, user)
    if not running:
        return {}
    row = connection.execute("SELECT payload FROM work_settings WHERE id=1").fetchone()
    if row is None:
        return running
    settings = parse_settings(json.loads(row["payload"]))
    current = next(
        (item for item in settings.active_sources if item.source_id == source.source_id), None
    )
    if current is None:
        return {}
    return {key: rule for key, rule in field_alert_scopes(current, user).items() if key in running}


def includes_item(source: SourceMapping, item: WorkItem, user: str | None) -> bool:
    """기존 본인 범위를 따라 취소 항목은 알림 기준에서 제외한다."""
    if isinstance(item, CalendarEvent) and item.cancelled:
        return False
    if not source.has_person_scope:
        return True
    people = (
        item.assignee_ids
        if isinstance(item, Task)
        else item.participant_ids
        if isinstance(item, CalendarEvent)
        else item.person_ids
    )
    return user is not None and canonical_identity(user) in map(canonical_identity, people)


def observed_baselines(connection: sqlite3.Connection, run: SyncRun, scope: str) -> dict[str, str]:
    """스테이징의 관측 ID로 한 규칙의 이전 값만 읽으며 재등장한 항목도 포함한다."""
    rows = connection.execute(
        """SELECT item_id,value_hash FROM source_alert_baselines
        WHERE source_id=? AND scope=? AND item_id IN
        (SELECT item_id FROM sync_staging WHERE run_id=?)""",
        (run.source_id, scope, run.id),
    )
    return {row["item_id"]: row["value_hash"] for row in rows}


def record_field_alerts(
    connection: sqlite3.Connection,
    run: SyncRun,
    items: Sequence[WorkItem],
    user: str | None,
    completed_at: str,
) -> None:
    """첫 관측은 비교 기준으로 저장하고 후속 변경만 같은 트랜잭션에서 예약한다."""
    source = run.source
    if source is None:
        return
    scopes = currently_saved_scopes(connection, source, user)
    baseline_counts = {
        row["scope"]: row["total"]
        for row in connection.execute(
            "SELECT scope,COUNT(*) AS total FROM source_alert_baselines WHERE source_id=? GROUP BY scope",
            (source.source_id,),
        )
    }
    now = datetime.fromisoformat(completed_at).timestamp()
    if not scopes and not baseline_counts:
        return
    removed_scopes = baseline_counts.keys() - scopes.keys()
    needs_cleanup = bool(removed_scopes)
    eligible = tuple(item for item in items if includes_item(source, item, user)) if scopes else ()
    for scope, rule in scopes.items():
        previous = observed_baselines(connection, run, scope) if scope in baseline_counts else {}
        # 기존 기준이 모두 이번 수집에 있으면 누락된 항목의 예약을 찾을 필요가 없다.
        if len(previous) < baseline_counts.get(scope, 0):
            needs_cleanup = True
        for item in eligible:
            digest = item.alert_values.get(rule.id)
            if digest is None:
                continue
            before = previous.pop(item.id, None)
            if before == digest:
                continue
            connection.execute(
                """INSERT INTO source_alert_baselines(source_id,scope,item_id,value_hash,kind_key) VALUES(?,?,?,?,?)
                ON CONFLICT(source_id,scope,item_id) DO UPDATE SET value_hash=excluded.value_hash""",
                (source.source_id, scope, item.id, digest, rule.notification_kind),
            )
            if before is None:
                continue
            invalidate_field_alert(connection, source.source_id, scope, item.id)
            if rule.accepts(digest):
                enqueue_field_alert(connection, run, item, scope, rule, digest, now)
        # 관측했지만 사람 범위에서 빠지거나 비교값이 사라진 항목만 기준에서 제거한다.
        for item_id in previous:
            invalidate_field_alert(connection, source.source_id, scope, item_id)
            connection.execute(
                "DELETE FROM source_alert_baselines WHERE source_id=? AND scope=? AND item_id=?",
                (source.source_id, scope, item_id),
            )
    if needs_cleanup:
        retire_unobserved_field_alerts(connection, run, scopes)
        connection.executemany(
            "DELETE FROM source_alert_baselines WHERE source_id=? AND scope=?",
            ((source.source_id, scope) for scope in removed_scopes),
        )


def retire_unobserved_field_alerts(
    connection: sqlite3.Connection, run: SyncRun, scopes: dict[str, SourceAlertRule]
) -> None:
    """이번 실행에 없거나 규칙이 해제된 항목은 미전달 예약만 한 번에 취소한다."""
    # 임시 누락은 기준값을 지우지 않는다. 이번 수집에서 새로 예약한 항목은 관측 ID에 있다.
    connection.execute(
        """UPDATE notification_jobs AS job SET state='obsolete'
        WHERE kind='source_change' AND state IN ('pending','delivering')
        AND EXISTS (
            SELECT 1 FROM source_alert_events AS event
            JOIN source_alert_baselines AS baseline
              ON baseline.source_id=event.source_id AND baseline.scope=job.source_key
              AND baseline.item_id=event.item_id
            WHERE event.job_id=job.id AND event.source_id=?
              AND (job.source_key NOT IN (SELECT value FROM json_each(?))
                   OR NOT EXISTS (SELECT 1 FROM sync_staging AS observed
                       WHERE observed.run_id=? AND observed.item_id=event.item_id))
        )""",
        (run.source_id, json.dumps(list(scopes)), run.id),
    )


def invalidate_field_alert(
    connection: sqlite3.Connection, source_id: str, scope: str, item_id: str
) -> None:
    """지금 값이나 사용자 범위와 달라진 대기 작업은 전달하지 않는다."""
    connection.execute(
        """UPDATE notification_jobs SET state='obsolete'
        WHERE kind='source_change' AND source_key=? AND state IN ('pending','delivering')
        AND id IN (SELECT job_id FROM source_alert_events WHERE source_id=? AND item_id=?)""",
        (scope, source_id, item_id),
    )


def enqueue_field_alert(
    connection: sqlite3.Connection,
    run: SyncRun,
    item: WorkItem,
    scope: str,
    rule: SourceAlertRule,
    digest: str,
    now: float,
) -> None:
    """수집 세대별 알림과 최신 값 확인에 필요한 근거를 저장한다."""
    job = notification_key(f"source-change:{scope}:{item.id}:{run.id}")
    title = item.title if not isinstance(item, Leave) else "항목 값 변경"
    connection.execute(
        """INSERT INTO notification_jobs(id,logical_key,kind,title,body,notion_url,due_at,expires_at,state,source_key)
        VALUES(?,?,'source_change',?,?,?, ?,?,'pending',?)""",
        (job, job, rule.name, title[:160], notion_link(item.url), now, now + 86400, scope),
    )
    connection.execute(
        """INSERT INTO source_alert_events(job_id,source_id,item_id,field_id,value_hash,mapping_hash,kind_key,person_scoped)
        VALUES(?,?,?,?,?,?,?,?)""",
        (
            job,
            run.source_id,
            item.id,
            rule.id,
            digest,
            run.mapping_hash,
            rule.notification_kind,
            int(run.source is not None and run.source.has_person_scope),
        ),
    )


def field_alert_current(row: dict[str, object], user: str | None) -> bool:
    """발송 직전 현재 자료와 값이 규칙 발동 때와 같은지 확인한다."""
    if row["current_availability"] != "present" or row["current_mapping"] != row["field_mapping"]:
        return False
    payload = row["current_payload"]
    if not isinstance(payload, str):
        return False
    value: object = json.loads(payload)
    if not isinstance(value, dict) or value.get("cancelled") is True:
        return False
    digests = value.get("alert_values")
    if not isinstance(digests, dict) or digests.get(row["field_id"]) != row["field_hash"]:
        return False
    if row["field_person_scoped"] == 0:
        return True
    if user is None:
        return False
    identity = canonical_identity(user)
    for role in ("assignee_ids", "participant_ids", "person_ids"):
        people = value.get(role)
        if isinstance(people, list):
            return any(
                isinstance(person, str) and canonical_identity(person) == identity
                for person in people
            )
    return False
