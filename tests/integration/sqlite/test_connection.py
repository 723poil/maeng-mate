"""SQLite 파일 생성과 읽기 전용 경계를 검증한다."""

from pathlib import Path

import pytest

from src.application.errors import SnapshotError
from src.infrastructure.sqlite.connection import Database


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
