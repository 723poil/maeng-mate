"""업무 설정의 명시적 JSON 가져오기와 DB 기반 실행을 조립한다."""

import json
from collections.abc import Mapping
from pathlib import Path

from src.application.errors import SnapshotError
from src.application.work_settings import (
    Settings,
    WorkSettingsError,
    empty_work_settings,
    is_initial_document,
    parse_settings,
)
from src.bootstrap.environment import ConfigError, database_path, required
from src.infrastructure.sqlite.connection import Database
from src.infrastructure.sqlite.work_settings import SQLiteWorkSettings


def load_work_settings(database: Database, *, prepare_categories: bool = False) -> Settings:
    """서버 시작에서만 이전 카테고리 구조를 갱신한다."""
    try:
        store = SQLiteWorkSettings(database)
        stored = store.prepare_categories() if prepare_categories else store.read_validated()
        if stored is None:
            raise ConfigError(
                "업무 설정을 먼저 가져오세요: settings import-work --file <JSON 경로>"
            )
        _, settings = stored
        return settings
    except (SnapshotError, WorkSettingsError):
        raise ConfigError(
            "DB의 업무 설정을 확인하세요. migrate 실행 여부와 JSON 구조를 확인하세요."
        ) from None


def initialize_work_settings(database: Database) -> None:
    """새 설치에만 빈 문서를 만들고 기존 수집 설정을 빈 값으로 덮지 않는다."""
    store = SQLiteWorkSettings(database)
    if store.read() is not None:
        return
    with database.transaction(read_only=True) as connection:
        existing = connection.execute("SELECT 1 FROM work_source_state LIMIT 1").fetchone()
        links = connection.execute("SELECT 1 FROM notion_page_link_preferences LIMIT 1").fetchone()
    if existing is not None or links is not None:
        raise ConfigError(
            "기존 업무 설정을 먼저 가져오세요: settings import-work --file <JSON 경로>"
        )
    store.initialize(empty_work_settings())


def import_work_settings(env: Mapping[str, str], path: Path) -> bool:
    """파일은 명시적 최초 이전에서만 읽고 기존 DB 설정이 있으면 그대로 둔다."""
    from src.bootstrap.instance import coordination_lock

    with coordination_lock("storage", shared=True):
        store = SQLiteWorkSettings(Database(database_path(required(env, "DATABASE_PATH"))))
        try:
            current = store.read()
            if current is not None and not is_initial_document(current):
                return False
            raw: object = json.loads(path.read_bytes())
            if not isinstance(raw, dict) or any(not isinstance(key, str) for key in raw):
                raise WorkSettingsError("invalid")
            payload: dict[str, object] = {key: value for key, value in raw.items()}
            parse_settings(payload)
            store.initialize(payload, replace_initial=True)
            return True
        except (OSError, ValueError, SnapshotError):
            raise ConfigError(
                "업무 설정 가져오기에 실패했습니다. JSON 구조와 DB 상태를 확인하세요."
            ) from None
