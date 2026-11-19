"""운영 파일과 분리한 임시 SQLite fixture다."""

from pathlib import Path

import pytest

from src.infrastructure.sqlite.connection import Database
from src.infrastructure.sqlite.schema import migrate


@pytest.fixture
async def db(tmp_path: Path) -> Database:
    """각 테스트가 독립된 파일을 사용하고 마이그레이션 재실행을 확인한다."""
    database = Database(str(tmp_path / "work.sqlite3"))
    await migrate(database)
    await migrate(database)
    return database
