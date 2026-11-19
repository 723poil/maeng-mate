"""일반·프로바이더 설정을 한 문서로 원자 저장한다."""

import json
import sqlite3
from dataclasses import replace

from src.application.runtime_settings import (
    RuntimeSettingsConflictError,
    parse_runtime_settings,
    runtime_settings_payload,
)
from src.domain.runtime_settings import RuntimeSettings
from src.infrastructure.sqlite.connection import Database


def read_settings(connection: sqlite3.Connection) -> RuntimeSettings | None:
    """같은 트랜잭션에서 설정 유무와 확정 버전을 확인한다."""
    row = connection.execute("SELECT payload FROM runtime_settings WHERE id=1").fetchone()
    if row is None:
        return None
    value: object = json.loads(row["payload"])
    return parse_runtime_settings(value)


class SQLiteRuntimeSettings:
    """명시적 최초 가져오기와 버전 조건부 저장을 구현한다."""

    def __init__(self, database: Database) -> None:
        """생성 시 파일이나 기본 설정을 만들지 않는다."""
        self._database = database

    def read(self) -> RuntimeSettings | None:
        """읽기 전용 연결로 조회하고 손상된 문서를 기본값으로 숨기지 않는다."""
        with self._database.transaction(read_only=True) as connection:
            return read_settings(connection)

    def initialize(self, value: RuntimeSettings) -> RuntimeSettings:
        """최초 실행만 삽입해 반복·동시 가져오기가 기존 설정을 유지하게 한다."""
        value = parse_runtime_settings(runtime_settings_payload(value))
        with self._database.transaction() as connection:
            current = read_settings(connection)
            if current is not None:
                return current
            saved = replace(value, revision=1)
            connection.execute(
                "INSERT INTO runtime_settings(id,payload) VALUES(1,?)",
                (json.dumps(runtime_settings_payload(saved)),),
            )
        return saved

    def save(self, value: RuntimeSettings) -> RuntimeSettings:
        """최신 편집과 버전0의 최초 명시 저장만 허용하고 충돌을 구분한다."""
        value = parse_runtime_settings(runtime_settings_payload(value))
        saved = replace(value, revision=value.revision + 1)
        conflict = False
        with self._database.transaction() as connection:
            current = read_settings(connection)
            if current is None and value.revision == 0:
                connection.execute(
                    "INSERT INTO runtime_settings(id,payload) VALUES(1,?)",
                    (json.dumps(runtime_settings_payload(saved)),),
                )
            elif current is None or current.revision != value.revision:
                conflict = True
            else:
                connection.execute(
                    "UPDATE runtime_settings SET payload=? WHERE id=1",
                    (json.dumps(runtime_settings_payload(saved)),),
                )
        # Database의 일반 저장 오류로 감싸지지 않도록 트랜잭션 밖에서 구분한다.
        if conflict:
            raise RuntimeSettingsConflictError("설정이 변경되었습니다. 다시 불러와 주세요.")
        return saved
