"""수집 실행 전에 DB 준비 상태를 확인한다."""

from src.application.errors import SnapshotError
from src.bootstrap.environment import ConfigError
from src.infrastructure.sqlite.connection import Database
from src.infrastructure.sqlite.schema import SCHEMA_VERSION


async def require_migrated_database(database: Database) -> None:
    """최고 버전이 현재 코드와 일치할 때만 업무 실행을 허용한다."""
    try:
        versions = await database.fetch("SELECT MAX(version) AS version FROM schema_versions")
        # 버전 이력은 누적되므로 현재 버전 행이 있어도 상위 버전이 우선한다.
        version = versions[0]["version"]
        if isinstance(version, int) and version > SCHEMA_VERSION:
            raise ConfigError("현재 앱보다 높은 버전의 DB입니다. 최신 앱으로 실행하세요.")
        if version != SCHEMA_VERSION:
            raise SnapshotError("storage_failed")
    except SnapshotError:
        raise ConfigError("DB 준비가 필요합니다. migrate 명령을 먼저 실행하세요.") from None
