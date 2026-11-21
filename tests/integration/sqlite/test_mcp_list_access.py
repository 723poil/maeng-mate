"""목록 권한의 기기 경계·이전·복원과 폐기를 실제 SQLite에서 검증한다."""

import hashlib
from dataclasses import replace
from pathlib import Path

import pytest

from src.application.mcp_access import McpSettingsService
from src.application.mcp_lists import ListAccessError, ListQueries
from src.application.work_settings import empty_work_settings
from src.bootstrap.clock import now
from src.infrastructure.sqlite.mcp_access import SQLiteMcpAccess
from src.infrastructure.sqlite.mcp_lists import SQLiteListAccess
from src.infrastructure.sqlite.portable_backup import (
    build_portable_database,
    prepare_portable_restore,
    preserve_device_access,
    validate_portable_database,
)
from src.infrastructure.sqlite.schema import migrate
from src.infrastructure.sqlite.work_reader import SQLiteWorkReader
from src.infrastructure.sqlite.work_settings import SQLiteWorkSettings
from tests.fixtures.mcp_lists import prepare


async def test_token_expiry_revocation_and_global_stop(tmp_path: Path) -> None:
    """전용 허용 범위는 만료·폐기·전체 중지 이후 재사용할 수 없다."""
    database, settings = await prepare(tmp_path / "device.sqlite3")
    access = SQLiteListAccess(database)
    service = ListQueries(settings, SQLiteWorkReader(database), access, now, 1200)
    issue = service.issue(
        {
            "name": "fixture",
            "expiresInDays": 1,
            "allRows": True,
            "sources": [{"sourceId": "list-free", "fields": ["title"]}],
        }
    )
    token = issue["token"]
    assert isinstance(token, str)
    digest = hashlib.sha256(token.encode()).hexdigest()
    assert len(access.authorize(digest)) == 1
    with database.transaction() as connection:
        connection.execute("UPDATE mcp_list_tokens SET expires_at=0")
    with pytest.raises(ListAccessError):
        access.authorize(digest)
    with database.transaction() as connection:
        connection.execute("UPDATE mcp_list_tokens SET expires_at=NULL")
    legacy = McpSettingsService(SQLiteMcpAccess(database), "http://fixture/mcp/")
    legacy.set_enabled(0, False)
    with pytest.raises(ListAccessError):
        access.authorize(digest)
    legacy.set_enabled(1, True)
    identifier = issue["id"]
    assert isinstance(identifier, str)
    access.revoke(identifier)
    with pytest.raises(ListAccessError):
        access.authorize(digest)


async def test_portable_backup_excludes_grants_and_restores_current_revocation(
    tmp_path: Path,
) -> None:
    """과거 업무 복원이 목록 접근 원문·허용 범위·폐기를 되살리지 않는다."""
    database, settings = await prepare(tmp_path / "device.sqlite3")
    SQLiteWorkSettings(database).initialize(empty_work_settings())
    access = SQLiteListAccess(database)
    service = ListQueries(settings, SQLiteWorkReader(database), access, now, 1200)
    issue = service.issue(
        {
            "name": "fixture",
            "expiresInDays": 1,
            "allRows": True,
            "sources": [{"sourceId": "list-free", "fields": ["title"]}],
        }
    )
    portable = tmp_path / "portable.sqlite3"
    build_portable_database(database.path, portable)
    assert validate_portable_database(portable) == 19
    from src.infrastructure.sqlite.connection import Database

    assert await Database(str(portable)).fetch("SELECT * FROM mcp_list_tokens") == []
    identifier = issue["id"]
    assert isinstance(identifier, str)
    access.revoke(identifier)
    preserve_device_access(database.path, portable)
    current = await Database(str(portable)).fetch("SELECT revoked_at FROM mcp_list_tokens")
    assert current[0]["revoked_at"] is not None
    assert len(await Database(str(portable)).fetch("SELECT * FROM item_snapshots")) == 9


@pytest.mark.parametrize("old_version", [17, 18])
async def test_old_backups_gain_empty_list_access_without_data_loss(
    tmp_path: Path, old_version: int
) -> None:
    """이전 스키마의 정상 업무를 보존하고 새 권한은 빈 상태로 준비한다."""
    database, _ = await prepare(tmp_path / "old.sqlite3")
    SQLiteWorkSettings(database).initialize(empty_work_settings())
    with database.transaction() as connection:
        connection.execute("DROP TABLE mcp_list_tokens")
        connection.execute("DELETE FROM schema_versions WHERE version>18")
        if old_version == 17:
            connection.execute("DROP TABLE source_status_options")
            connection.execute("DELETE FROM schema_versions WHERE version=18")
    assert validate_portable_database(database.path) == old_version
    prepare_portable_restore(database.path)
    assert validate_portable_database(database.path) == 19
    assert await database.fetch("SELECT * FROM mcp_list_tokens") == []
    assert len(await database.fetch("SELECT * FROM item_snapshots")) == 9
    await migrate(database)


async def test_source_origin_change_never_expands_existing_grant(tmp_path: Path) -> None:
    """같은 논리 소스 ID를 다른 원본으로 바꿔도 과거 동의를 재사용하지 않는다."""
    database, settings = await prepare(tmp_path / "device.sqlite3")
    access = SQLiteListAccess(database)
    service = ListQueries(settings, SQLiteWorkReader(database), access, now, 1200)
    issue = service.issue(
        {
            "name": "fixture",
            "expiresInDays": 1,
            "allRows": True,
            "sources": [{"sourceId": "list-free", "fields": ["title"]}],
        }
    )
    token = issue["token"]
    assert isinstance(token, str)
    changed = replace(
        settings, sources=tuple(replace(s, origin_id="other-origin") for s in settings.sources)
    )
    service = ListQueries(changed, SQLiteWorkReader(database), access, now, 1200)
    digest = hashlib.sha256(token.encode()).hexdigest()
    result = await service.search(digest, {})
    assert result["items"] == []
    assert result["configuration"] == {"status": "partial", "reasons": ["source_binding_changed"]}
