"""논리 알림과 채널별 전달 실행권·결과를 분리한다."""

import json
import sqlite3
from collections.abc import Callable, Mapping
from uuid import uuid4

from src.application.notifications import NotificationJob
from src.domain.preferences import Preferences
from src.domain.source_alerts import is_source_alert_kind
from src.infrastructure.sqlite.notification_keys import departure_key, notification_key
from src.infrastructure.sqlite.rule_delivery import DeliveryRules, channel_kinds


def delivery_kinds(
    connection: sqlite3.Connection,
    row: sqlite3.Row,
    qa_scopes: tuple[str, ...],
    preferences: Preferences,
) -> tuple[str, ...]:
    """저장된 사실로 알림 종류를 분류하며 제목 문자열에 의존하지 않는다."""
    kind = row["kind"]
    if kind == "departure":
        for minutes in preferences.departure_offsets:
            if row["logical_key"] == departure_key(row["day"], minutes):
                return (f"departure_{minutes}",)
        return ()
    if kind == "task":
        event = connection.execute(
            "SELECT kinds FROM task_changes WHERE id=?", (row["task_change_id"],)
        ).fetchone()
        if event is None:
            return ()
        raw: object = json.loads(event["kinds"])
        if not isinstance(raw, list):
            raise ValueError("알림 종류를 확인하세요.")
        prefix = "qa" if row["source_key"] in qa_scopes else "task"
        return tuple(
            f"{prefix}_{item}"
            for item in raw
            if isinstance(item, str) and item in {"assigned", "status", "due"}
        )
    if kind == "source_change":
        rule = connection.execute(
            "SELECT kind_key FROM source_alert_events WHERE job_id=?", (row["id"],)
        ).fetchone()
        return (rule["kind_key"],) if rule is not None else ()
    return (kind,) if isinstance(kind, str) else ()


def logical_completed(
    connection: sqlite3.Connection, logical_key: str, source_key: str | None = None
) -> bool:
    """기존 대상 채널이 모두 요청·불명확 상태일 때만 논리 알림을 끝난 것으로 본다."""
    legacy = connection.execute(
        """SELECT 1 FROM notification_jobs AS previous WHERE logical_key=? AND (? IS NULL OR previous.source_key=?)
        AND (state IN ('requested','unknown') OR (state='obsolete' AND token IS NOT NULL))
        AND NOT EXISTS(SELECT 1 FROM notification_deliveries d WHERE d.job_id=previous.id)""",
        (logical_key, source_key, source_key),
    ).fetchone()
    if legacy is not None:
        return True
    channels = connection.execute(
        """SELECT DISTINCT delivery.channel FROM notification_deliveries AS delivery
        JOIN notification_jobs AS job ON job.id=delivery.job_id WHERE job.logical_key=? AND (? IS NULL OR job.source_key=?)""",
        (logical_key, source_key, source_key),
    ).fetchall()
    if not channels:
        return False
    for row in channels:
        delivered = connection.execute(
            """SELECT 1 FROM notification_deliveries AS delivery
            JOIN notification_jobs AS job ON job.id=delivery.job_id
            WHERE job.logical_key=? AND delivery.channel=? AND delivery.state IN ('requested','unknown')""",
            (logical_key, row["channel"]),
        ).fetchone()
        if delivered is None:
            return False
    return True


def prepare_deliveries(
    connection: sqlite3.Connection,
    row: sqlite3.Row,
    preferences: Preferences,
    channels: tuple[str, ...],
    qa_scopes: tuple[str, ...],
    allowed_types: tuple[str, ...] | None = None,
    group: str | None = None,
    titles: Mapping[str, str] | None = None,
    rules: DeliveryRules | None = None,
    aliases: Mapping[str, str] | None = None,
) -> None:
    """한 알림의 대상 채널을 한 번 확정해 이후 채널 추가로 과거 알림을 보내지 않는다."""
    kinds = delivery_kinds(connection, row, qa_scopes, preferences)
    if (
        row["kind"] == "sync_error"
        and allowed_types
        and all(is_source_alert_kind(key) for key in allowed_types)
    ):
        kinds = allowed_types
    if allowed_types is not None:
        kinds = tuple(kind for kind in kinds if kind in allowed_types)
    if aliases:
        # 이전 종류의 구독·해제도 같은 표에 연결된 현재 규칙 ID로 관리한다.
        kinds = tuple(aliases.get(kind, kind) for kind in kinds)
    legacy = connection.execute(
        """SELECT 1 FROM notification_jobs AS previous WHERE logical_key=? AND id<>?
        AND (state IN ('requested','unknown') OR (state='obsolete' AND token IS NOT NULL))
        AND NOT EXISTS(SELECT 1 FROM notification_deliveries d WHERE d.job_id=previous.id)""",
        (row["logical_key"], row["id"]),
    ).fetchone()
    enabled: dict[str, tuple[str, ...]] = {}
    for channel in channels:
        selected = tuple(
            kind
            for kind in channel_kinds(kinds, channel, rules)
            if preferences.notification_enabled(kind, group)
        )
        if legacy is not None or not preferences.permits(channel, selected, group):
            continue
        attempted = connection.execute(
            """SELECT 1 FROM notification_deliveries AS delivery
            JOIN notification_jobs AS previous ON previous.id=delivery.job_id
            WHERE previous.logical_key=? AND previous.id<>? AND delivery.channel=?
            AND delivery.state IN ('requested','unknown')""",
            (row["logical_key"], row["id"], channel),
        ).fetchone()
        # 이미 전달한 채널만 제외한다. 정정 전 미전달 채널은 최신 예약을 받을 수 있다.
        if attempted is None:
            enabled[channel] = selected
    for channel, selected in enabled.items():
        connection.execute(
            "INSERT INTO notification_deliveries(id,job_id,channel,kinds,state) VALUES(?,?,?,?,'pending') ON CONFLICT DO NOTHING",
            (notification_key(f"{row['id']}:{channel}"), row["id"], channel, json.dumps(selected)),
        )
    chosen_title = next(
        (
            titles[kind]
            for kind in kinds
            if titles is not None
            and kind in titles
            and preferences.notification_enabled(kind, group)
        ),
        row["title"],
    )
    connection.execute(
        "UPDATE notification_jobs SET state=?,title=? WHERE id=?",
        ("delivering" if enabled else "cancelled", chosen_title, row["id"]),
    )


def finish_deliveries(connection: sqlite3.Connection, now: float) -> None:
    """불명확한 전송은 보존하고 모든 채널이 끝난 알림만 집계 확정한다."""
    connection.execute(
        "UPDATE notification_deliveries SET state='unknown' WHERE state='delivering' AND claimed_at<?",
        (now - 30,),
    )
    # 이전 실행권의 요청 결과를 기다리던 새 예약은 확정 성공·불명확일 때만 취소한다.
    connection.execute(
        """UPDATE notification_deliveries SET state='cancelled' WHERE state='pending'
        AND EXISTS(SELECT 1 FROM notification_deliveries AS other
            JOIN notification_jobs AS previous ON previous.id=other.job_id
            JOIN notification_jobs AS current ON current.id=notification_deliveries.job_id
            WHERE other.id<>notification_deliveries.id
            AND other.channel=notification_deliveries.channel AND previous.logical_key=current.logical_key
            AND other.state IN ('requested','unknown'))"""
    )
    # 대기 전달에 연결된 예약만 PK로 확인해 완료 이력을 매 주기 순회하지 않는다.
    connection.execute(
        """UPDATE notification_deliveries SET state='cancelled' WHERE state='pending'
        AND EXISTS(SELECT 1 FROM notification_jobs AS job
            WHERE job.id=notification_deliveries.job_id AND job.state<>'delivering')"""
    )
    connection.execute(
        """UPDATE notification_deliveries SET state='expired' WHERE state='pending'
        AND EXISTS(SELECT 1 FROM notification_jobs AS job
            WHERE job.id=notification_deliveries.job_id AND job.expires_at<?)""",
        (now,),
    )
    connection.execute(
        """UPDATE notification_jobs SET state=CASE
        WHEN EXISTS(SELECT 1 FROM notification_deliveries d WHERE d.job_id=notification_jobs.id AND d.state='unknown') THEN 'unknown'
        WHEN EXISTS(SELECT 1 FROM notification_deliveries d WHERE d.job_id=notification_jobs.id AND d.state='requested') THEN 'requested'
        WHEN EXISTS(SELECT 1 FROM notification_deliveries d WHERE d.job_id=notification_jobs.id AND d.state='failed') THEN 'failed'
        ELSE 'cancelled' END
        WHERE state IN ('delivering','obsolete') AND EXISTS(SELECT 1 FROM notification_deliveries d WHERE d.job_id=notification_jobs.id)
        AND NOT EXISTS(SELECT 1 FROM notification_deliveries d WHERE d.job_id=notification_jobs.id AND d.state IN ('pending','delivering'))"""
    )


def claim_delivery(
    connection: sqlite3.Connection,
    now: float,
    source_keys: tuple[str, ...] | None,
    channels: tuple[str, ...],
    make_job: Callable[[sqlite3.Connection, sqlite3.Row, str], NotificationJob],
) -> tuple[NotificationJob, ...]:
    """채널마다 별도 토큰을 부여해 성공한 채널을 재전송하지 않는다."""
    if not channels:
        return ()
    placeholders = ",".join("?" for _ in channels)
    scope = ""
    scope_values: tuple[object, ...] = ()
    if source_keys is not None:
        if source_keys:
            keys = ",".join("?" for _ in source_keys)
            scope = f" AND (j.kind='departure' OR j.source_key IN ({keys}))"
            scope_values = tuple(source_keys)
        else:
            scope = " AND j.kind='departure'"
    row = connection.execute(
        """SELECT d.id,d.channel,d.kinds,j.source_key,j.title,j.body,j.notion_url FROM notification_deliveries d
        JOIN notification_jobs j ON j.id=d.job_id
        WHERE d.state='pending' AND d.revoked=0 AND j.state='delivering' AND j.due_at<=? AND j.expires_at>=?
        AND NOT EXISTS(SELECT 1 FROM notification_deliveries AS other
            JOIN notification_jobs AS previous ON previous.id=other.job_id
            WHERE other.id<>d.id AND other.channel=d.channel AND previous.logical_key=j.logical_key
            AND (other.state IN ('delivering','requested','unknown')
                OR (other.state='pending' AND previous.state='delivering'
                    AND (previous.due_at<j.due_at OR (previous.due_at=j.due_at AND previous.id<j.id)))))"""
        + f" AND d.channel IN ({placeholders})"
        + scope
        + " ORDER BY CASE j.kind WHEN 'departure' THEN 0 WHEN 'meeting' THEN 1 WHEN 'sync_error' THEN 2 ELSE 3 END,j.due_at,d.id LIMIT 1",
        (now, now, *channels, *scope_values),
    ).fetchone()
    if row is None:
        return ()
    token = uuid4().hex
    connection.execute(
        "UPDATE notification_deliveries SET state='delivering',token=?,claimed_at=? WHERE id=?",
        (token, now, row["id"]),
    )
    return (make_job(connection, row, token),)
