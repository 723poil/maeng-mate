"""MCP 토큰 해시와 만료·폐기·최근 사용을 SQLite에서 관리한다."""

import math
import sqlite3

from src.application.mcp_access import McpAccessError
from src.domain.mcp_access import McpAccessSnapshot, McpAccessToken
from src.infrastructure.sqlite.connection import Database


def _time(value: object) -> float | None:
    """SQLite 시점 값과 미사용 상태를 엄격하게 구분한다."""
    if value is None:
        return None
    if (
        type(value) not in (int, float)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
    ):
        raise ValueError
    return float(value)


def _token(row: sqlite3.Row) -> McpAccessToken:
    """해시를 제외한 열만 공개 모델로 변환한다."""
    identifier: object = row["id"]
    name: object = row["name"]
    created = _time(row["created_at"])
    if not isinstance(identifier, str) or not isinstance(name, str) or created is None:
        raise ValueError
    return McpAccessToken(
        identifier,
        name,
        created,
        _time(row["expires_at"]),
        _time(row["revoked_at"]),
        _time(row["last_used_at"]),
    )


def _read(connection: sqlite3.Connection) -> McpAccessSnapshot:
    """사용 선택과 목록을 하나의 트랜잭션에서 읽는다."""
    row = connection.execute("SELECT revision,enabled FROM mcp_preferences WHERE id=1").fetchone()
    return McpAccessSnapshot(
        int(row["revision"]) if row else 0,
        bool(row["enabled"]) if row else True,
        tuple(
            _token(row)
            for row in connection.execute(
                "SELECT id,name,created_at,expires_at,revoked_at,last_used_at FROM mcp_access_tokens ORDER BY created_at DESC,id"
            )
        ),
    )


class SQLiteMcpAccess:
    """쓰기 권한 없는 AI 접근과 관리 키를 저장 단계에서도 분리한다."""

    def __init__(self, database: Database) -> None:
        """파일 생성 없이 기존 DB만 주입한다."""
        self._database = database

    def read(self) -> McpAccessSnapshot:
        """조회로 기본 행을 만들지 않는다."""
        with self._database.transaction(read_only=True) as connection:
            return _read(connection)

    def set_enabled(self, revision: int, enabled: bool) -> McpAccessSnapshot:
        """변경한 사용 선택은 다음 인증부터 바로 적용한다."""
        conflict = False
        with self._database.transaction() as connection:
            current = _read(connection)
            if current.revision != revision:
                conflict = True
                result = current
            else:
                connection.execute(
                    "INSERT INTO mcp_preferences(id,revision,enabled) VALUES(1,?,?) ON CONFLICT(id) DO UPDATE SET revision=excluded.revision,enabled=excluded.enabled",
                    (revision + 1, int(enabled)),
                )
                result = _read(connection)
        if conflict:
            raise McpAccessError("conflict")
        return result

    def insert(self, token: McpAccessToken, digest: str) -> None:
        """원문 대신 해시만 추가한다."""
        with self._database.transaction() as connection:
            connection.execute(
                "INSERT INTO mcp_access_tokens(id,name,digest,created_at,expires_at) VALUES(?,?,?,?,?)",
                (token.id, token.name, digest, token.created_at, token.expires_at),
            )

    def revoke(self, identifier: str, now: float) -> None:
        """최근 사용과 발급 기록은 보존하고 접근만 폐기한다."""
        missing = False
        with self._database.transaction() as connection:
            row = connection.execute(
                "SELECT id FROM mcp_access_tokens WHERE id=?", (identifier,)
            ).fetchone()
            if row is None:
                missing = True
            else:
                connection.execute(
                    "UPDATE mcp_access_tokens SET revoked_at=COALESCE(revoked_at,?) WHERE id=?",
                    (now, identifier),
                )
        if missing:
            raise McpAccessError("missing")

    def authorize(self, digest: str, now: float) -> bool:
        """폐기·만료 경계·전체 중지를 같은 쓰기 순서에서 판정한다."""
        with self._database.transaction() as connection:
            enabled = connection.execute(
                "SELECT enabled FROM mcp_preferences WHERE id=1"
            ).fetchone()
            if enabled is not None and not enabled["enabled"]:
                return False
            row = connection.execute(
                "SELECT id FROM mcp_access_tokens WHERE digest=? AND revoked_at IS NULL AND (expires_at IS NULL OR expires_at>?)",
                (digest, now),
            ).fetchone()
            if row is None:
                return False
            connection.execute(
                "UPDATE mcp_access_tokens SET last_used_at=? WHERE id=?", (now, row["id"])
            )
            return True
