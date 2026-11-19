"""수집 실행 전에 DB 준비 상태를 확인한다."""

from src.application.errors import SnapshotError
from src.bootstrap.environment import ConfigError
from src.infrastructure.sqlite.connection import Database
from src.infrastructure.sqlite.schema import SCHEMA_VERSION


async def require_migrated_database(database: Database) -> None:
    """이전·미생성 DB는 외부 조회나 실행권 변경 전에 안내한다."""
    try:
        versions = await database.fetch(
            "SELECT version FROM schema_versions WHERE version=?", (SCHEMA_VERSION,)
        )
        if not versions:
            raise SnapshotError("storage_failed")
    except SnapshotError:
        raise ConfigError("DB 준비가 필요합니다. migrate 명령을 먼저 실행하세요.") from None
