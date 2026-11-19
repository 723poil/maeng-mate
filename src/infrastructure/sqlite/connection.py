"""작업 스레드별 SQLite 연결과 명시적 트랜잭션을 제공한다."""

import asyncio
import os
import sqlite3
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path

from src.application.day_ports import DayConflictError
from src.application.errors import SnapshotError
from src.infrastructure.sqlite.paths import validate_database_path


class Database:
    """연결을 공유하지 않고 같은 작업 스레드에서 열고 닫는다."""

    def __init__(self, path: str) -> None:
        """생성자에서는 파일과 디렉터리를 만들지 않는다."""
        self.path = Path(validate_database_path(path))

    def prepare(self) -> None:
        """명시적 마이그레이션에서만 개인 DB 파일을 생성한다."""
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            try:
                descriptor = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
            except FileExistsError:
                pass
            else:
                os.close(descriptor)
            connection = self._connect(read_only=False)
            try:
                connection.execute("PRAGMA journal_mode=WAL")
            finally:
                connection.close()
        except (OSError, sqlite3.Error):
            raise SnapshotError("storage_failed") from None

    def _connect(self, *, read_only: bool) -> sqlite3.Connection:
        """사용자가 URI 옵션을 주입하지 못하게 파일 경로에서 내부 URI를 만든다."""
        address = self.path.as_uri() + ("?mode=ro" if read_only else "?mode=rw")
        connection = sqlite3.connect(
            address,
            uri=True,
            timeout=5,
            isolation_level=None,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        if read_only:
            connection.execute("PRAGMA query_only=ON")
        return connection

    @contextmanager
    def transaction(self, *, read_only: bool = False) -> Iterator[sqlite3.Connection]:
        """짧은 쓰기를 직렬화하고 읽기는 파일과 실행 상태를 변경하지 않는다."""
        connection = None
        try:
            connection = self._connect(read_only=read_only)
            connection.execute("BEGIN" if read_only else "BEGIN IMMEDIATE")
            yield connection
            connection.execute("COMMIT")
        except BaseException as error:
            if connection is not None and connection.in_transaction:
                connection.rollback()
            if isinstance(error, (SnapshotError, DayConflictError)) or not isinstance(
                error, Exception
            ):
                raise
            raise SnapshotError("storage_failed") from None
        finally:
            if connection is not None:
                connection.close()

    async def fetch(
        self, statement: str, parameters: Sequence[object] = ()
    ) -> list[dict[str, object]]:
        """조회 SQL은 이벤트 루프 밖의 읽기 전용 연결에서 실행한다."""
        return await asyncio.to_thread(self._fetch, statement, parameters)

    def _fetch(self, statement: str, parameters: Sequence[object]) -> list[dict[str, object]]:
        """조회 연결을 다른 스레드로 넘기지 않는다."""
        with self.transaction(read_only=True) as connection:
            return [dict(row) for row in connection.execute(statement, parameters).fetchall()]
