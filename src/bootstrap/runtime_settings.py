"""일반·프로바이더 설정의 최초 가져오기와 실행 시 우선순위를 정한다."""

import re
from collections.abc import Mapping

from src.application.errors import SnapshotError
from src.application.runtime_settings import parse_runtime_settings
from src.bootstrap.environment import ConfigError, database_path, optional, required
from src.domain.runtime_settings import RuntimeSettings
from src.infrastructure.sqlite.connection import Database
from src.infrastructure.sqlite.runtime_settings import SQLiteRuntimeSettings


def settings_from_environment(env: Mapping[str, str]) -> RuntimeSettings:
    """허용한 비밀값 없는 항목만 이전 후보로 만든다."""
    policy = optional(env, "WORK_RELEVANCE_POLICY") or "participation"
    age = optional(env, "SNAPSHOT_MAX_AGE_SECONDS") or "1200"
    if not re.fullmatch(r"[0-9]+", age):
        raise ConfigError("자료 유효시간을 확인하세요: SNAPSHOT_MAX_AGE_SECONDS")
    try:
        return parse_runtime_settings(
            {
                "revision": 0,
                "general": {"relevancePolicy": policy, "snapshotMaxAgeSeconds": int(age)},
                "providers": {"notion": {"userId": optional(env, "NOTION_USER_ID")}},
            }
        )
    except ValueError:
        raise ConfigError("이전할 일반·Notion 설정을 확인하세요.") from None


def load_runtime_environment(env: Mapping[str, str]) -> dict[str, str]:
    """초기화한 DB 값을 우선하며 미초기화일 때만 기존 환경값을 사용한다."""
    values = dict(env)
    path = optional(env, "DATABASE_PATH")
    if path is None:
        return values
    try:
        stored = SQLiteRuntimeSettings(Database(database_path(path))).read()
    except SnapshotError:
        raise ConfigError(
            "DB 설정을 읽지 못했습니다. migrate 실행 여부와 DB 상태를 확인하세요."
        ) from None
    if stored is None:
        return values
    values.update(
        WORK_RELEVANCE_POLICY=stored.general.relevance_policy,
        SNAPSHOT_MAX_AGE_SECONDS=str(stored.general.snapshot_max_age_seconds),
        NOTION_USER_ID=stored.notion.user_id or "",
    )
    return values


def _import_runtime_settings(env: Mapping[str, str]) -> bool:
    """검증된 환경값을 한 번 가져오며 기존 DB 값·비밀 파일은 바꾸지 않는다."""
    store = SQLiteRuntimeSettings(Database(database_path(required(env, "DATABASE_PATH"))))
    try:
        if store.read() is not None:
            return False
        store.initialize(settings_from_environment(env))
        return True
    except SnapshotError:
        raise ConfigError(
            "DB 설정 가져오기에 실패했습니다. migrate 실행 여부와 DB 상태를 확인하세요."
        ) from None


def import_runtime_settings(env: Mapping[str, str]) -> bool:
    """데이터 위치 전환과 겹치지 않도록 기존 저장 작업의 잠금을 공유한다."""
    from src.bootstrap.instance import coordination_lock

    with coordination_lock("storage", shared=True):
        return _import_runtime_settings(env)
