"""SQLite의 영속 실행·정상 스냅샷 스키마다."""

import asyncio
import sqlite3

from src.infrastructure.sqlite.connection import Database

SCHEMA_VERSION = 18

SOURCE_STATUS_OPTIONS_TABLE = """CREATE TABLE IF NOT EXISTS source_status_options(
      source_id TEXT PRIMARY KEY REFERENCES work_source_state(source_id),
      options_json TEXT NOT NULL)"""

NOTIFICATION_TABLE = """CREATE TABLE IF NOT EXISTS notification_jobs(
  id TEXT PRIMARY KEY, logical_key TEXT NOT NULL, kind TEXT NOT NULL CHECK(kind IN ('calendar','departure','task','meeting','sync_error','source_change')),
  title TEXT NOT NULL, body TEXT NOT NULL, notion_url TEXT,due_at REAL NOT NULL, expires_at REAL NOT NULL,
  state TEXT NOT NULL CHECK(state IN ('pending','delivering','requested','failed','unknown','cancelled','obsolete','expired')),
  day TEXT, revision INTEGER, token TEXT, claimed_at REAL, source_key TEXT,
  task_change_id TEXT REFERENCES task_changes(id),
  meeting_source_id TEXT REFERENCES work_source_state(source_id), meeting_item_id TEXT,
  meeting_mapping_hash TEXT, meeting_start_at REAL,
  sync_source_id TEXT REFERENCES work_source_state(source_id), sync_mapping_hash TEXT,
  sync_failure_run_id INTEGER REFERENCES sync_runs(id),
  CHECK((kind='task' AND task_change_id IS NOT NULL) OR (kind<>'task' AND task_change_id IS NULL)),
  CHECK(kind<>'meeting' OR (meeting_source_id IS NOT NULL AND meeting_item_id IS NOT NULL
      AND meeting_mapping_hash IS NOT NULL AND meeting_start_at IS NOT NULL)),
  CHECK(kind<>'sync_error' OR (sync_source_id IS NOT NULL AND sync_mapping_hash IS NOT NULL
      AND sync_failure_run_id IS NOT NULL)))"""

DELIVERIES_TABLE = """CREATE TABLE IF NOT EXISTS notification_deliveries(
      id TEXT PRIMARY KEY, job_id TEXT NOT NULL REFERENCES notification_jobs(id),
      channel TEXT NOT NULL,kinds TEXT NOT NULL,
      state TEXT NOT NULL CHECK(state IN ('pending','delivering','requested','failed','unknown','cancelled','expired')),
      token TEXT, claimed_at REAL, request_started_at REAL, revoked INTEGER NOT NULL DEFAULT 0 CHECK(revoked IN (0,1)),
      UNIQUE(job_id,channel))"""

TABLES = (
    "CREATE TABLE IF NOT EXISTS schema_versions(version INTEGER PRIMARY KEY)",
    """CREATE TABLE IF NOT EXISTS work_source_state(
      source_id TEXT PRIMARY KEY, kind TEXT NOT NULL CHECK(kind IN ('task','meeting','leave')),
      generation INTEGER NOT NULL DEFAULT 0, lease_until REAL, mapping_hash TEXT,
      last_completed_at TEXT)""",
    """CREATE TABLE IF NOT EXISTS sync_runs(
      id INTEGER PRIMARY KEY AUTOINCREMENT, source_id TEXT NOT NULL REFERENCES work_source_state(source_id),
      generation INTEGER NOT NULL, mapping_hash TEXT NOT NULL, started_at TEXT NOT NULL,
      completed_at TEXT, status TEXT NOT NULL CHECK(status IN ('running','succeeded','failed','abandoned')),
      failure_code TEXT CHECK(failure_code IN ('source_failed','storage_failed')), UNIQUE(source_id,generation))""",
    """CREATE TABLE IF NOT EXISTS sync_staging(
      run_id INTEGER NOT NULL REFERENCES sync_runs(id), item_id TEXT NOT NULL CHECK(length(item_id)>0),
      payload TEXT NOT NULL, semantic_hash TEXT NOT NULL, PRIMARY KEY(run_id,item_id))""",
    """CREATE TABLE IF NOT EXISTS item_snapshots(
      source_id TEXT NOT NULL REFERENCES work_source_state(source_id), item_id TEXT NOT NULL CHECK(length(item_id)>0),
      payload TEXT NOT NULL, semantic_hash TEXT NOT NULL, mapping_hash TEXT NOT NULL,
      last_seen_run_id INTEGER NOT NULL REFERENCES sync_runs(id), observed_at TEXT NOT NULL,
      availability TEXT NOT NULL CHECK(availability IN ('present','unconfirmed_missing')),
      PRIMARY KEY(source_id,item_id))""",
    """CREATE TABLE IF NOT EXISTS sync_schedules(
      source_id TEXT PRIMARY KEY REFERENCES work_source_state(source_id),
      interval_seconds INTEGER NOT NULL CHECK(interval_seconds>=60),
      next_run_at REAL NOT NULL, last_finished_at REAL,
      retry_count INTEGER NOT NULL DEFAULT 0 CHECK(retry_count BETWEEN 0 AND 3),
      retry_pending INTEGER NOT NULL DEFAULT 0 CHECK(retry_pending IN (0,1)),
      error_code TEXT, mapping_hash TEXT)""",
    """CREATE TABLE IF NOT EXISTS day_records(
      day TEXT PRIMARY KEY, revision INTEGER NOT NULL CHECK(revision>0), payload TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS day_record_revisions(
      day TEXT NOT NULL REFERENCES day_records(day), revision INTEGER NOT NULL CHECK(revision>0),
      payload TEXT NOT NULL, PRIMARY KEY(day,revision))""",
    """CREATE TABLE IF NOT EXISTS task_changes(
      id TEXT PRIMARY KEY,source_id TEXT NOT NULL REFERENCES work_source_state(source_id),item_id TEXT NOT NULL,
      run_id INTEGER NOT NULL REFERENCES sync_runs(id),user_key TEXT NOT NULL,mapping_hash TEXT NOT NULL,
      kinds TEXT NOT NULL,before_json TEXT,after_json TEXT NOT NULL,after_hash TEXT NOT NULL,
      title TEXT NOT NULL,description TEXT NOT NULL,observed_at TEXT NOT NULL,
      UNIQUE(run_id,item_id,user_key))""",
    """CREATE TABLE IF NOT EXISTS collection_preferences(
      id INTEGER PRIMARY KEY CHECK(id=1),
      interval_seconds INTEGER NOT NULL CHECK(interval_seconds BETWEEN 60 AND 86400))""",
    """CREATE TABLE IF NOT EXISTS app_preferences(
      id INTEGER PRIMARY KEY CHECK(id=1), payload TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS runtime_settings(
      id INTEGER PRIMARY KEY CHECK(id=1), payload TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS mcp_preferences(
      id INTEGER PRIMARY KEY CHECK(id=1),revision INTEGER NOT NULL CHECK(revision>0),enabled INTEGER NOT NULL CHECK(enabled IN (0,1)))""",
    """CREATE TABLE IF NOT EXISTS mcp_access_tokens(
      id TEXT PRIMARY KEY,name TEXT NOT NULL,digest TEXT NOT NULL UNIQUE,
      created_at REAL NOT NULL,expires_at REAL,revoked_at REAL,last_used_at REAL)""",
    """CREATE TABLE IF NOT EXISTS notion_page_link_preferences(
      id INTEGER PRIMARY KEY CHECK(id=1),revision INTEGER NOT NULL CHECK(revision>0),payload TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS work_settings(
      id INTEGER PRIMARY KEY CHECK(id=1),revision INTEGER NOT NULL CHECK(revision>0),
      payload TEXT NOT NULL CHECK(json_valid(payload) AND json_type(payload)='object'))""",
    NOTIFICATION_TABLE,
    DELIVERIES_TABLE,
    """CREATE TABLE IF NOT EXISTS source_alert_baselines(
      source_id TEXT NOT NULL REFERENCES work_source_state(source_id),
      scope TEXT NOT NULL, item_id TEXT NOT NULL, value_hash TEXT NOT NULL, kind_key TEXT NOT NULL,
      PRIMARY KEY(source_id,scope,item_id))""",
    """CREATE TABLE IF NOT EXISTS source_alert_events(
      job_id TEXT PRIMARY KEY REFERENCES notification_jobs(id) ON DELETE CASCADE,
      source_id TEXT NOT NULL REFERENCES work_source_state(source_id),
      item_id TEXT NOT NULL, field_id TEXT NOT NULL, value_hash TEXT NOT NULL,
      mapping_hash TEXT NOT NULL, kind_key TEXT NOT NULL, person_scoped INTEGER NOT NULL CHECK(person_scoped IN (0,1)))""",
    SOURCE_STATUS_OPTIONS_TABLE,
)

INDEXES = (
    "CREATE INDEX IF NOT EXISTS notification_delivery_state_idx ON notification_deliveries(state,job_id)",
    "CREATE INDEX IF NOT EXISTS notification_due_idx ON notification_jobs(state,due_at)",
    "CREATE INDEX IF NOT EXISTS notification_logical_idx ON notification_jobs(logical_key)",
    "CREATE INDEX IF NOT EXISTS sync_runs_source_started_idx ON sync_runs(source_id,started_at)",
    "CREATE INDEX IF NOT EXISTS task_changes_item_idx ON task_changes(source_id,item_id,user_key)",
    "CREATE INDEX IF NOT EXISTS source_alert_events_item_idx ON source_alert_events(source_id,item_id)",
)


async def migrate(database: Database) -> None:
    """명시적 명령에서 파일과 스키마를 준비한다."""
    await asyncio.to_thread(_migrate, database)


def _migrate(database: Database) -> None:
    """전체 DDL을 한 쓰기 트랜잭션으로 적용한다."""
    database.prepare()
    with database.transaction() as connection:
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
