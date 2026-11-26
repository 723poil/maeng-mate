"""출시한 휴대용 DB의 기준 DDL과 버전별 추가 정의를 보존한다."""

MCP_LIST_TOKENS_TABLE = """CREATE TABLE IF NOT EXISTS mcp_list_tokens(
      id TEXT PRIMARY KEY,name TEXT NOT NULL,digest TEXT NOT NULL UNIQUE,
      created_at REAL NOT NULL,expires_at REAL,revoked_at REAL,last_used_at REAL,
      grants TEXT NOT NULL CHECK(json_valid(grants) AND json_type(grants)='array'))"""

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

V17_TABLES = (
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
)

V17_INDEXES = (
    "CREATE INDEX IF NOT EXISTS notification_delivery_state_idx ON notification_deliveries(state,job_id)",
    "CREATE INDEX IF NOT EXISTS notification_due_idx ON notification_jobs(state,due_at)",
    "CREATE INDEX IF NOT EXISTS notification_logical_idx ON notification_jobs(logical_key)",
    "CREATE INDEX IF NOT EXISTS sync_runs_source_started_idx ON sync_runs(source_id,started_at)",
    "CREATE INDEX IF NOT EXISTS task_changes_item_idx ON task_changes(source_id,item_id,user_key)",
    "CREATE INDEX IF NOT EXISTS source_alert_events_item_idx ON source_alert_events(source_id,item_id)",
)

# 출시한 버전의 정의는 고정하고 다음 버전은 별도의 DDL로 확장한다.
V18_TABLES = (*V17_TABLES, SOURCE_STATUS_OPTIONS_TABLE)
V19_TABLES = (*V18_TABLES, MCP_LIST_TOKENS_TABLE)

PORTABLE_SCHEMAS = {
    17: (*V17_TABLES, *V17_INDEXES),
    18: (*V18_TABLES, *V17_INDEXES),
    19: (*V19_TABLES, *V17_INDEXES),
}
# 키는 변환이 끝난 뒤 기록할 버전이다.
PORTABLE_UPGRADES = {18: (SOURCE_STATUS_OPTIONS_TABLE,), 19: (MCP_LIST_TOKENS_TABLE,)}
