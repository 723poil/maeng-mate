"""화면과 알림 설정을 같은 버전으로 원자 저장한다."""

import json
import sqlite3
from collections.abc import Mapping
from dataclasses import replace

from src.application.preferences import (
    PreferencesConflictError,
    parse_preferences,
    preferences_payload,
)
from src.domain.preferences import Preferences
from src.infrastructure.sqlite.connection import Database


def read_preferences(
    connection: sqlite3.Connection, channels: tuple[str, ...] = ("macos",)
) -> Preferences:
    """호출자의 트랜잭션 안에서 설정과 알림 실행권의 일관성을 유지한다."""
    row = connection.execute("SELECT payload FROM app_preferences WHERE id=1").fetchone()
    if row is None:
        return Preferences(channels=tuple(channel for channel in ("macos",) if channel in channels))
    value: object = json.loads(row["payload"])
    return parse_preferences(value, channels)


def write_preferences(connection: sqlite3.Connection, value: Preferences) -> None:
    """검증한 환경설정 문서를 호출자의 트랜잭션으로 저장한다."""
    connection.execute(
        "INSERT INTO app_preferences(id,payload) VALUES(1,?) ON CONFLICT(id) DO UPDATE SET payload=excluded.payload",
        (json.dumps(preferences_payload(value)),),
    )


def revoke_muted_deliveries(
    connection: sqlite3.Connection, value: Preferences, groups: Mapping[str, str]
) -> None:
    """꺼진 알림의 전달을 취소하고 다시 켜도 이전 실행권을 복원하지 않는다."""
    rows = connection.execute(
        "SELECT d.id,d.channel,d.kinds,j.source_key FROM notification_deliveries d JOIN notification_jobs j ON j.id=d.job_id WHERE d.state IN ('pending','delivering') AND d.revoked=0"
    ).fetchall()
    for row in rows:
        kinds: object = json.loads(row["kinds"])
        if not isinstance(kinds, list) or not all(isinstance(kind, str) for kind in kinds):
            raise ValueError("알림 종류를 확인하세요.")
        selected = tuple(kind for kind in kinds if isinstance(kind, str))
        if not value.permits(row["channel"], selected, groups.get(row["source_key"])):
            connection.execute(
                """UPDATE notification_deliveries SET revoked=1,
                state=CASE WHEN state='pending' THEN 'cancelled' ELSE state END WHERE id=?""",
                (row["id"],),
            )


class SQLitePreferences:
    """설정 미기록과 저장 실패를 구별하고 버전 충돌을 보존한다."""

    def __init__(
        self,
        database: Database,
        channels: tuple[str, ...] = ("macos",),
        *,
        notification_groups: Mapping[str, str] | None = None,
    ) -> None:
        """설치되어 사용 가능한 알림 채널만 허용한다."""
        self._database = database
        self._channels = channels
        self._notification_groups = dict(notification_groups or {})

    def read(self) -> Preferences:
        """읽기만 수행하며 기본값을 DB에 쓰지 않는다."""
        with self._database.transaction(read_only=True) as connection:
            return read_preferences(connection, self._channels)

    def save(self, value: Preferences) -> Preferences:
        """설정 변경과 아직 요청하지 않은 채널의 취소를 함께 확정한다."""
        value = parse_preferences(preferences_payload(value), self._channels)
        conflict = False
        saved = replace(value, revision=value.revision + 1)
        with self._database.transaction() as connection:
            current = read_preferences(connection, self._channels)
            if current.revision != value.revision:
                conflict = True
            else:
                write_preferences(connection, saved)
                revoke_muted_deliveries(connection, saved, self._notification_groups)
        if conflict:
            raise PreferencesConflictError("설정이 변경되었습니다. 다시 불러와 주세요.")
        return saved
