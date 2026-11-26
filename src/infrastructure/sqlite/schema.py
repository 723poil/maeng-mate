"""SQLite의 영속 실행·정상 스냅샷 스키마다."""

import asyncio
import sqlite3

from src.application.errors import SnapshotError
from src.infrastructure.sqlite.connection import Database
from src.infrastructure.sqlite.schema_versions import (
    DELIVERIES_TABLE,
    NOTIFICATION_TABLE,
    V17_INDEXES,
    V19_TABLES,
)

SCHEMA_VERSION = 19

TABLES = V19_TABLES
INDEXES = V17_INDEXES


async def migrate(database: Database) -> None:
    """명시적 명령에서 파일과 스키마를 준비한다."""
    await asyncio.to_thread(_migrate, database)


def _migrate(database: Database) -> None:
    """전체 DDL을 한 쓰기 트랜잭션으로 적용한다."""
    database.prepare()
    with database.transaction() as connection:
        # 신규 DB에는 버전 테이블이 없다. 기존 DB는 DDL 전에 같은 쓰기 잠금에서 검사한다.
        has_versions = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_versions'"
        ).fetchone()
        if has_versions is not None:
            version = connection.execute("SELECT MAX(version) FROM schema_versions").fetchone()[0]
            if version is not None and version > SCHEMA_VERSION:
                raise SnapshotError("database_version_newer")
        for statement in TABLES:
            connection.execute(statement)
        upgrade_notification_jobs(connection)
        upgrade_sync_schedules(connection)
        upgrade_notification_deliveries(connection)
        upgrade_source_alert_jobs(connection)
        # 테이블을 교체하는 이전 단계가 끝난 뒤 현재 테이블에 인덱스를 연결한다.
        for statement in INDEXES:
            connection.execute(statement)
        connection.executemany(
            "INSERT INTO schema_versions(version) VALUES(?) ON CONFLICT DO NOTHING",
            ((version,) for version in range(1, SCHEMA_VERSION + 1)),
        )


def upgrade_notification_jobs(connection: sqlite3.Connection) -> None:
    """기존 알림의 결과·실행권·업무 연결을 보존해 수집 오류 지원 테이블로 교체한다."""
    columns = {row["name"] for row in connection.execute("PRAGMA table_info(notification_jobs)")}
    if "sync_failure_run_id" in columns:
        if "notion_url" not in columns:
            connection.execute("ALTER TABLE notification_jobs ADD COLUMN notion_url TEXT")
        return
    connection.execute(NOTIFICATION_TABLE.replace("notification_jobs", "notification_jobs_v8", 1))
    fields = "id,logical_key,kind,title,body,due_at,expires_at,state,day,revision,token,claimed_at,source_key"
    if "task_change_id" in columns:
        fields += ",task_change_id"
    if "meeting_start_at" in columns:
        fields += ",meeting_source_id,meeting_item_id,meeting_mapping_hash,meeting_start_at"
    connection.execute(
        f"INSERT INTO notification_jobs_v8({fields}) SELECT {fields} FROM notification_jobs"
    )
    connection.execute("DROP TABLE notification_jobs")
    connection.execute("ALTER TABLE notification_jobs_v8 RENAME TO notification_jobs")


def upgrade_sync_schedules(connection: sqlite3.Connection) -> None:
    """v8의 예약 시각과 사용자 주기를 보존하고 재시도 상태만 추가한다."""
    columns = {row["name"] for row in connection.execute("PRAGMA table_info(sync_schedules)")}
    additions = {
        "retry_count": "INTEGER NOT NULL DEFAULT 0 CHECK(retry_count BETWEEN 0 AND 3)",
        "retry_pending": "INTEGER NOT NULL DEFAULT 0 CHECK(retry_pending IN (0,1))",
        "error_code": "TEXT",
        "mapping_hash": "TEXT",
    }
    for name, definition in additions.items():
        if name not in columns:
            connection.execute(f"ALTER TABLE sync_schedules ADD COLUMN {name} {definition}")
    connection.execute(
        """UPDATE sync_schedules SET mapping_hash=(SELECT state.mapping_hash
        FROM work_source_state AS state WHERE state.source_id=sync_schedules.source_id)
        WHERE mapping_hash IS NULL"""
    )


def upgrade_notification_deliveries(connection: sqlite3.Connection) -> None:
    """기존 채널 실행권을 보존하고 승인 취소·외부 요청 시작 표식을 보완한다."""
    columns = {
        row["name"] for row in connection.execute("PRAGMA table_info(notification_deliveries)")
    }
    if "request_started_at" not in columns:
        connection.execute("ALTER TABLE notification_deliveries ADD COLUMN request_started_at REAL")
    if "revoked" not in columns:
        connection.execute(
            "ALTER TABLE notification_deliveries ADD COLUMN revoked INTEGER NOT NULL DEFAULT 0 CHECK(revoked IN (0,1))"
        )


def upgrade_source_alert_jobs(connection: sqlite3.Connection) -> None:
    """기존 작업과 채널 결과를 원자적으로 옮겨 새 알림 종류를 허용한다."""
    sql = connection.execute(
        "SELECT sql FROM sqlite_master WHERE name='notification_jobs'"
    ).fetchone()["sql"]
    if "'source_change'" in sql:
        return
    connection.execute(NOTIFICATION_TABLE.replace("notification_jobs", "notification_jobs_v17", 1))
    fields = (
        "id,logical_key,kind,title,body,notion_url,due_at,expires_at,state,day,revision,token,claimed_at,source_key,"
        "task_change_id,meeting_source_id,meeting_item_id,meeting_mapping_hash,meeting_start_at,"
        "sync_source_id,sync_mapping_hash,sync_failure_run_id"
    )
    connection.execute(
        f"INSERT INTO notification_jobs_v17({fields}) SELECT {fields} FROM notification_jobs"
    )
    connection.execute("CREATE TEMP TABLE delivery_backup AS SELECT * FROM notification_deliveries")
    connection.execute("DROP TABLE notification_deliveries")
    connection.execute("DROP TABLE notification_jobs")
    connection.execute("ALTER TABLE notification_jobs_v17 RENAME TO notification_jobs")
    connection.execute(DELIVERIES_TABLE)
    deliveries = "id,job_id,channel,kinds,state,token,claimed_at,request_started_at,revoked"
    connection.execute(
        f"INSERT INTO notification_deliveries({deliveries}) SELECT {deliveries} FROM delivery_backup"
    )
    connection.execute("DROP TABLE delivery_backup")
