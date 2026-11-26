"""성공한 수집 안에서 속성 변경을 감지하고 최신 값에만 알림을 허용한다."""

import json
import sqlite3
from collections.abc import Iterator, Sequence
from datetime import UTC, datetime

from src.application.models import SyncRun
from src.application.notion_links import notion_link
from src.application.work_settings import Settings, parse_settings
from src.domain.event_classification import EVENT_CLASSIFICATION
from src.domain.models import CalendarEvent, Leave, SourceMapping, Task, WorkItem
from src.domain.notification_timing import calendar_alert_expiry
from src.domain.source_alerts import (
    CHANGE_BODY,
    MEETING_BODY,
    AlertSchedule,
    SourceAlertRule,
    alert_body_token,
    alert_value_key,
)
from src.domain.work_dates import canonical_identity, seoul_clock, seoul_datetime
from src.infrastructure.sqlite.decoding import decode_item
from src.infrastructure.sqlite.notification_deliveries import logical_completed
from src.infrastructure.sqlite.notification_keys import failure_rule_scope, notification_key
from src.infrastructure.sqlite.serialization import (
    alert_scope_fingerprint,
    fingerprint,
    source_fingerprint,
)
from src.infrastructure.sqlite.task_changes import notification_body


def field_alert_scopes(source: SourceMapping, user: str | None) -> dict[str, SourceAlertRule]:
    """규칙, 원본 매핑, 본인 범위를 한 발송 범위로 묶는다."""
    identity = canonical_identity(user) if user else None
    mapping = source_fingerprint(source)
    return {
        fingerprint(
            (
                source.source_id,
                mapping,
                identity if source.has_person_scope or rule.member == "self" else None,
                rule.id,
                rule.column_id,
                rule.comparison_kind,
                rule.target,
                *((rule.member, rule.item_filter) if rule.member or rule.item_filter else ()),
                *((rule.schedule.mode, rule.schedule.value) if rule.schedule else ()),
                *((digest,) if (digest := alert_scope_fingerprint(rule)) else ()),
            )
        ): rule
        for rule in source.alert_rules
        if rule.enabled
        and (
            user is not None
            or (rule.member != "self" and (rule.member is not None or not source.has_person_scope))
        )
    }


def saved_rule_bindings(
    settings: Settings,
) -> dict[
    tuple[str, str],
    tuple[str, str, str | None, AlertSchedule | None, str | None, str | None, str | None, str],
]:
    """규칙 이름을 제외한 실제 조건을 저장값과 실행값 사이에서 비교한다."""
    return {
        (source.source_id, rule.notification_kind): (
            rule.column_id,
            rule.comparison_kind,
            rule.target,
            rule.schedule,
            rule.member,
            rule.item_filter,
            alert_scope_fingerprint(rule),
            source_fingerprint(source),
        )
        for source in settings.active_sources
        for rule in source.alert_rules
        if rule.enabled
    }


def retire_changed_rules(connection: sqlite3.Connection, before: Settings, after: Settings) -> None:
    """규칙을 끄거나 바꾸면 이전 예약과 기준을 설정 저장과 함께 무효화한다."""

    def failure_scopes(settings: Settings) -> set[str]:
        """수집 실패 조건이 달라지거나 해제되면 예전 오류의 실행권도 함께 취소한다."""
        return {
            failure_rule_scope(source.source_id, source_fingerprint(source), rule.id, rule.target)
            for source in settings.active_sources
            for rule in source.failure_rules
        }

    connection.executemany(
        "UPDATE notification_jobs SET state='obsolete' WHERE kind='sync_error' AND source_key=? AND state IN ('pending','delivering')",
        ((key,) for key in failure_scopes(before) - failure_scopes(after)),
    )
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


def seed_converted_baselines(
    connection: sqlite3.Connection, run: SyncRun, user: str | None
) -> None:
    """전환 직후 첫 수집에서도 이전 스냅샷 이후의 실제 배정·값 변경만 알린다."""
    source = run.source
    if source is None or user is None:
        return
    scopes = {
        key: rule
        for key, rule in field_alert_scopes(source, user).items()
        if rule.previous_kind and rule.schedule is None
    }
    if not scopes:
        return
    for row in connection.execute(
        "SELECT DISTINCT scope FROM source_alert_baselines WHERE source_id=?", (run.source_id,)
    ):
        scopes.pop(row["scope"], None)
    if not scopes:
        return

    user_id = canonical_identity(user)

    # 빈 정상 수집 뒤 첫 신규 행도 구별하도록 빈 항목 ID를 규칙 초기화 표식으로 사용한다.
    def rows() -> Iterator[tuple[str, str, str, str, str]]:
        """비교에 쓰던 원래 역할 값만 읽고 사용자·매핑이 다른 이력은 섞지 않는다."""
        for scope, rule in scopes.items():
            yield run.source_id, scope, "", "", rule.notification_kind
        for row in connection.execute(
            "SELECT payload FROM item_snapshots WHERE source_id=? AND mapping_hash=?",
            (run.source_id, run.mapping_hash),
        ):
            item = decode_item(json.loads(row["payload"]), source)
            for scope, rule in scopes.items():
                if rule.member == "self":
                    people = (
                        item.assignee_ids
                        if isinstance(item, Task)
                        else item.participant_ids
                        if isinstance(item, CalendarEvent)
                        else ()
                    )
                    value = "present" if user_id in map(canonical_identity, people) else "absent"
                elif isinstance(item, Task) and includes_item(source, item, user_id):
                    if rule.previous_kind in {"task_status", "qa_status"}:
                        value = item.status or ""
                    elif rule.previous_kind in {"task_due", "qa_due"}:
                        value = (
                            " ~ ".join(x for x in (item.due.start, item.due.end) if x)
                            if item.due
                            else ""
                        )
                    else:
                        continue
                else:
                    continue
                yield (
                    run.source_id,
                    scope,
                    item.id,
                    alert_value_key(value, rule.comparison_kind),
                    rule.notification_kind,
                )

    connection.executemany(
        "INSERT INTO source_alert_baselines(source_id,scope,item_id,value_hash,kind_key) VALUES(?,?,?,?,?)",
        rows(),
    )


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
            "SELECT scope,COUNT(NULLIF(item_id,'')) AS total FROM source_alert_baselines WHERE source_id=? GROUP BY scope",
            (source.source_id,),
        )
    }
    observed = datetime.fromisoformat(completed_at)
    now = observed.timestamp()
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
        # 빈 목록을 처음 수집한 뒤 새로 배정된 행도 알릴 수 있도록 규칙 초기화를 기록한다.
        initialized = scope in baseline_counts
        if rule.member is not None and not initialized:
            connection.execute(
                "INSERT INTO source_alert_baselines(source_id,scope,item_id,value_hash,kind_key) VALUES(?,?,'','',?)",
                (source.source_id, scope, rule.notification_kind),
            )
        kinds = {field: kind for field, _, kind in rule.scope_bindings}
        for item in items if rule.member is not None else eligible:
            if isinstance(item, CalendarEvent) and item.cancelled:
                continue
            if (
                rule.source_scopes is None
                and rule.item_filter == "meeting"
                and (not isinstance(item, CalendarEvent) or item.event_type != "meeting")
            ):
                continue
            if rule.scope is not None:
                # 날짜 조건은 값 변경이면 수집일, 예약이면 알림 예정일을 서울 시간으로 평가한다.
                basis = item.alert_due_at.get(rule.id, observed)
                values = {
                    field: item.display_values[key]
                    for field, key, _ in rule.scope_bindings
                    if key in item.display_values
                }
                # 일정의 최종 분류는 숨긴 칼럼과 무관하게 스냅샷에 저장된다.
                if EVENT_CLASSIFICATION in kinds and isinstance(item, CalendarEvent):
                    values[EVENT_CLASSIFICATION] = item.category or ""
                local = seoul_datetime(basis)
                if values.keys() != kinds.keys() or not rule.scope.includes(
                    values, kinds, local.date() if local else None
                ):
                    continue
            expires = now + 86400
            if rule.item_filter == "upcoming":
                if (
                    not isinstance(item, CalendarEvent)
                    or (limit := calendar_alert_expiry(item.period, observed)) is None
                ):
                    continue
                expires = limit
            digest = item.alert_values.get(rule.id)
            if digest is None:
                continue
            before = previous.pop(item.id, None)
            if before != digest:
                connection.execute(
                    """INSERT INTO source_alert_baselines(source_id,scope,item_id,value_hash,kind_key) VALUES(?,?,?,?,?)
                    ON CONFLICT(source_id,scope,item_id) DO UPDATE SET value_hash=excluded.value_hash""",
                    (source.source_id, scope, item.id, digest, rule.notification_kind),
                )
                if before is not None:
                    invalidate_field_alert(connection, source.source_id, scope, item.id)
            if rule.schedule is not None:
                # 처음 본 미래 시각과 누락 후 다시 나타난 미래 시각도 한 번만 예약한다.
                due = item.alert_due_at.get(rule.id)
                if due is None or (expiry := rule.schedule.expires_at(due)) is None:
                    continue
                if due.timestamp() >= now or (
                    rule.item_filter == "meeting"
                    and rule.schedule.mode == "before_start"
                    and expiry > observed
                ):
                    enqueue_field_alert(
                        connection,
                        run,
                        item,
                        scope,
                        rule,
                        digest,
                        due.timestamp(),
                        expiry.timestamp(),
                        scheduled=True,
                    )
            elif (
                before != digest
                and (before is not None or (rule.member is not None and initialized))
                and rule.accepts(digest)
            ):
                enqueue_field_alert(connection, run, item, scope, rule, digest, now, expires)
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
    due: float,
    expires: float,
    *,
    scheduled: bool = False,
) -> None:
    """수집 세대별 알림과 최신 값 확인에 필요한 근거를 저장한다."""
    job = notification_key(f"source-change:{scope}:{item.id}:{run.id}")
    title = item.title if not isinstance(item, Leave) else "항목 값 변경"
    body = title[:160]
    token = rule.delivery.body_token if rule.delivery else None
    token = token or alert_body_token(rule.column_id, rule.previous_kind)
    if isinstance(item, Task) and token == CHANGE_BODY:
        change = connection.execute(
            "SELECT description FROM task_changes WHERE run_id=? AND item_id=? AND source_id=?",
            (run.id, item.id, run.source_id),
        ).fetchone()
        if change is not None:
            body = notification_body(title, change["description"])
    elif token == MEETING_BODY and rule.schedule:
        # 사전 예약의 기준 칼럼을 변경하면 본문 시각도 같은 칼럼의 원본 시작값을 쓴다.
        if rule.schedule.mode == "before_start":
            event_start = datetime.fromtimestamp(due + int(rule.schedule.value) * 60, UTC)
            body = f"{seoul_clock(event_start)} · {body}"
        elif run.source is not None:
            period = next(
                (
                    item.calendar_periods[col.value_key]
                    for col in run.source.storage_layout
                    if col.property_id == rule.column_id and col.value_key in item.calendar_periods
                ),
                None,
            )
            if period and period.kind == "instant":
                body = f"{seoul_clock(datetime.fromisoformat(period.start))} · {body}"
    origin = run.source.origin if run.source is not None else run.source_id
    # 규칙별 관측과 취소는 뷰에 남기고, 실제로 같은 원본 변경을 본 채널만 전달을 공유한다.
    logical = fingerprint(
        ("source-change", origin, item.id, rule.id, rule.column_id, item.source_edited_at, digest)
    )
    if scheduled:
        logical = fingerprint(("scheduled", origin, item.id, rule.id, digest, due))
        # 이전 버전의 예약은 원본 표 ID에 속한다. 실행권과 채널 이력은 보존한다.
        for prior in connection.execute(
            """SELECT job.id,job.source_key,job.logical_key FROM source_alert_events AS event
            JOIN notification_jobs AS job ON job.id=event.job_id
            WHERE event.source_id=? AND event.item_id=? AND event.field_id=?
            AND event.value_hash=? AND job.due_at=? AND job.logical_key<>?""",
            (origin, item.id, rule.id, digest, due, logical),
        ).fetchall():
            if prior["logical_key"] == notification_key(
                f"scheduled:{prior['source_key']}:{item.id}:{digest}"
            ):
                connection.execute(
                    "UPDATE notification_jobs SET logical_key=? WHERE id=?", (logical, prior["id"])
                )
        # 같은 시각을 다시 수집하면 대기 중인 안내만 갱신한다. 취소된 미래 예약은 복구할 수 있다.
        current = connection.execute(
            """SELECT id,state,body,notion_url FROM notification_jobs
            WHERE logical_key=? AND source_key=? AND state IN ('pending','delivering') LIMIT 1""",
            (logical, scope),
        ).fetchone()
        if current is not None:
            url = notion_link(item.url)
            if current["state"] == "pending" and (
                current["body"] != body or current["notion_url"] != url
            ):
                connection.execute(
                    "UPDATE notification_jobs SET body=?,notion_url=? WHERE id=?",
                    (body, url, current["id"]),
                )
            return
        if (
            rule.previous_kind == "meeting"
            and rule.schedule
            and rule.schedule.mode == "before_start"
        ):
            # 같은 회의의 옛 실행권이나 접수 이력이 있으면 새 UUID로 중복 발송하지 않는다.
            start = due + int(rule.schedule.value) * 60
            prior = connection.execute(
                """SELECT logical_key,state FROM notification_jobs WHERE kind='meeting'
                AND meeting_source_id=? AND meeting_item_id=? AND meeting_mapping_hash=? AND meeting_start_at=?
                AND state IN ('pending','delivering','requested','unknown')""",
                (run.source_id, item.id, run.mapping_hash, start),
            ).fetchall()
            if any(
                row["state"] in ("pending", "delivering")
                or logical_completed(connection, row["logical_key"])
                for row in prior
            ):
                return
        if logical_completed(connection, logical, scope):
            return
    connection.execute(
        """INSERT INTO notification_jobs(id,logical_key,kind,title,body,notion_url,due_at,expires_at,state,source_key)
        VALUES(?,?,'source_change',?,?,?, ?,?,'pending',?)""",
        (
            job,
            logical,
            rule.name,
            body,
            notion_link(item.url),
            due,
            expires,
            scope,
        ),
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
            int(run.source is not None and run.source.has_person_scope and rule.member is None),
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
