"""목록 전용 토큰은 개인 MCP의 해시 저장소와 분리한다."""

import hashlib
import json
import secrets
import time
from uuid import uuid4

from src.application.errors import SnapshotError
from src.application.mcp_lists import ListAccessError, ListGrant
from src.infrastructure.sqlite.connection import Database


def decode_grants(value: object) -> tuple[ListGrant, ...]:
    """저장된 허용 범위가 손상되면 빈 허용이나 전체 허용으로 바꾸지 않는다."""
    if not isinstance(value, str):
        raise ValueError
    raw: object = json.loads(value)
    if not isinstance(raw, list) or not raw:
        raise ValueError
    result = []
    for entry in raw:
        if not isinstance(entry, dict) or set(entry) != {"sourceId", "binding", "fields"}:
            raise ValueError
        identifier, binding, fields = entry["sourceId"], entry["binding"], entry["fields"]
        if (
            not isinstance(identifier, str)
            or not isinstance(binding, str)
            or not isinstance(fields, dict)
            or not fields
        ):
            raise ValueError
        pairs: list[tuple[str, str]] = []
        for key, target in fields.items():
            if not isinstance(key, str) or not isinstance(target, str):
                raise ValueError
            pairs.append((key, target))
        result.append(ListGrant(identifier, binding, tuple(pairs)))
    return tuple(result)


class SQLiteListAccess:
    """구버전 앱이 목록 토큰을 개인 조회 토큰으로 해석할 수 없게 한다."""

    def __init__(self, database: Database) -> None:
        """준비된 기기 DB를 주입하며 토큰 원문은 보관하지 않는다."""
        self.database = database

    def authorize(self, digest: str) -> tuple[ListGrant, ...]:
        """매번 만료·폐기·전체 중지를 확인한 뒤 요청 시각을 기록한다."""
        with self.database.transaction() as connection:
            enabled = connection.execute(
                "SELECT enabled FROM mcp_preferences WHERE id=1"
            ).fetchone()
            row = connection.execute(
                "SELECT id,grants FROM mcp_list_tokens WHERE digest=? AND revoked_at IS NULL AND (expires_at IS NULL OR expires_at>?)",
                (digest, time.time()),
            ).fetchone()
            if row is None or (enabled is not None and not enabled["enabled"]):
                grants = None
            else:
                try:
                    grants = decode_grants(row["grants"])
                except ValueError:
                    raise SnapshotError("storage_failed") from None
                connection.execute(
                    "UPDATE mcp_list_tokens SET last_used_at=? WHERE id=?", (time.time(), row["id"])
                )
        if grants is None:
            raise ListAccessError()
        return grants

    def issue(
        self, name: str, days: int | None, grants: tuple[ListGrant, ...]
    ) -> dict[str, object]:
        """원문을 한 번 반환하고 기존 개인 토큰 테이블에는 쓰지 않는다."""
        identifier, token, created = (
            uuid4().hex,
            "mm_list_" + secrets.token_urlsafe(32),
            time.time(),
        )
        expiry = created + days * 86400 if days is not None else None
        payload = json.dumps(
            [
                {"sourceId": g.source_id, "binding": g.binding, "fields": dict(g.fields)}
                for g in grants
            ]
        )
        with self.database.transaction() as connection:
            connection.execute(
                "INSERT INTO mcp_list_tokens(id,name,digest,created_at,expires_at,grants) VALUES(?,?,?,?,?,?)",
                (
                    identifier,
                    name,
                    hashlib.sha256(token.encode()).hexdigest(),
                    created,
                    expiry,
                    payload,
                ),
            )
        return {"id": identifier, "token": token, "expiresAt": expiry}

    def tokens(self) -> list[dict[str, object]]:
        """관리 화면에도 원문·해시·원본 속성 연결은 공개하지 않는다."""
        with self.database.transaction(read_only=True) as connection:
            result = []
            for row in connection.execute(
                "SELECT id,name,created_at,expires_at,revoked_at,last_used_at,grants FROM mcp_list_tokens ORDER BY created_at DESC,id"
            ):
                grants = decode_grants(row["grants"])
                result.append(
                    {
                        "id": row["id"],
                        "name": row["name"],
                        "createdAt": row["created_at"],
                        "expiresAt": row["expires_at"],
                        "revokedAt": row["revoked_at"],
                        "lastUsedAt": row["last_used_at"],
                        "sources": [
                            {"sourceId": g.source_id, "fields": [key for key, _ in g.fields]}
                            for g in grants
                        ],
                    }
                )
            return result

    def revoke(self, identifier: str) -> None:
        """존재하는 토큰만 폐기하고 반복 폐기는 같은 결과로 유지한다."""
        with self.database.transaction() as connection:
            changed = connection.execute(
                "UPDATE mcp_list_tokens SET revoked_at=COALESCE(revoked_at,?) WHERE id=?",
                (time.time(), identifier),
            )
            missing = changed.rowcount != 1
        if missing:
            raise ListAccessError()
