"""같은 SQLite 트랜잭션에서 알림 구독과 화면 설정을 읽고 쓴다."""

import json
import sqlite3

from src.application.preferences import parse_preferences, preferences_payload
from src.domain.preferences import Preferences


def read_preferences(
    connection: sqlite3.Connection, channels: tuple[str, ...] = ("macos",)
) -> Preferences:
    """호출자의 트랜잭션 안에서 설정과 알림 실행권의 일관성을 유지한다."""
    row = connection.execute("SELECT payload FROM app_preferences WHERE id=1").fetchone()
    if row is None:
        return Preferences()
    value: object = json.loads(row["payload"])
    return parse_preferences(value, channels)


def write_preferences(connection: sqlite3.Connection, value: Preferences) -> None:
    """검증한 환경설정 문서를 호출자의 트랜잭션으로 저장한다."""
    connection.execute(
        "INSERT INTO app_preferences(id,payload) VALUES(1,?) ON CONFLICT(id) DO UPDATE SET payload=excluded.payload",
        (json.dumps(preferences_payload(value)),),
    )
