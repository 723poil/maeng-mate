"""SQLite 파일 생성과 읽기 전용 경계를 검증한다."""

from pathlib import Path

import pytest

from src.application.errors import SnapshotError
from src.bootstrap.environment import ConfigError
from src.bootstrap.storage import require_migrated_database
from src.infrastructure.sqlite.connection import Database
from src.infrastructure.sqlite.schema import SCHEMA_VERSION, migrate


async def test_read_query_cannot_write(db: Database) -> None:
    """조회용 연결에 쓰기 SQL이 전달돼도 DB 상태를 바꿀 수 없다."""
    before = await db.fetch("SELECT version FROM schema_versions ORDER BY version")
    with pytest.raises(SnapshotError):
        await db.fetch("INSERT INTO schema_versions(version) VALUES(999)")
    assert await db.fetch("SELECT version FROM schema_versions ORDER BY version") == before


async def test_missing_database_read_does_not_create_files(tmp_path: Path) -> None:
    """조회 실패 때문에 빈 DB 파일이나 디렉터리를 만들지 않는다."""
    path = tmp_path / "not-created" / "work.sqlite3"
    with pytest.raises(SnapshotError):
        await Database(str(path)).fetch("SELECT 1")
    assert not path.parent.exists()


@pytest.mark.parametrize("include_current", [False, True])
async def test_newer_database_rejected_without_changes(
    tmp_path: Path, include_current: bool
) -> None:
    """상위 버전만 있거나 현재 버전이 함께 있어도 거부하고 스키마·사용자 행을 보존한다."""
    database = Database(str(tmp_path / "future.sqlite3"))
    if include_current:
        await migrate(database)
    else:
        database.prepare()
    with database.transaction() as connection:
        connection.execute(
            "CREATE TABLE IF NOT EXISTS schema_versions(version INTEGER PRIMARY KEY)"
        )
        connection.execute("INSERT INTO schema_versions VALUES(?)", (SCHEMA_VERSION + 1,))
        connection.execute("CREATE TABLE future_records(id INTEGER PRIMARY KEY, payload TEXT)")
        connection.execute("INSERT INTO future_records VALUES(1, 'fixture-user-row')")
        before = list(connection.iterdump())
        schema_version = connection.execute("PRAGMA schema_version").fetchone()[0]
    with pytest.raises(ConfigError, match="최신 앱"):
        await require_migrated_database(database)
    with pytest.raises(SnapshotError, match="database_version_newer"):
        await migrate(database)
    with database.transaction(read_only=True) as connection:
        assert list(connection.iterdump()) == before
        assert connection.execute("PRAGMA schema_version").fetchone()[0] == schema_version


@pytest.mark.parametrize("version", [None, 0, 1, SCHEMA_VERSION])
async def test_database_version_readiness_and_migration(
    tmp_path: Path, version: int | None
) -> None:
    """신규·빈 이력·구버전은 이전 후 준비되고 현재 버전은 재실행해도 보존된다."""
    database = Database(str(tmp_path / "versions.sqlite3"))
    if version is not None:
        database.prepare()
        with database.transaction() as connection:
            connection.execute("CREATE TABLE schema_versions(version INTEGER PRIMARY KEY)")
            if version:
                connection.execute("INSERT INTO schema_versions VALUES(?)", (version,))
            connection.execute("CREATE TABLE fixture_records(payload TEXT)")
            connection.execute("INSERT INTO fixture_records VALUES('fixture-user-row')")
    if version == SCHEMA_VERSION:
        await require_migrated_database(database)
    else:
        with pytest.raises(ConfigError, match="migrate"):
            await require_migrated_database(database)
    if version is None:
        assert not database.path.exists()
    await migrate(database)
    await migrate(database)
    await require_migrated_database(database)
    assert await database.fetch("SELECT MAX(version) AS version FROM schema_versions") == [
        {"version": SCHEMA_VERSION}
    ]
    if version is not None:
        assert await database.fetch("SELECT payload FROM fixture_records") == [
            {"payload": "fixture-user-row"}
        ]
