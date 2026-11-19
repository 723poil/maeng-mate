"""기기 종속 권한과 과거 페이지를 휴대용 DB 사본에서 분리한다."""

import sqlite3
from contextlib import closing
from pathlib import Path

from src.infrastructure.sqlite.runtime_settings import read_settings
from src.infrastructure.sqlite.schema import INDEXES, SCHEMA_VERSION, TABLES
from src.infrastructure.sqlite.work_settings import read_document

_DEVICE_TABLES = ("mcp_preferences", "mcp_access_tokens")
_SCHEMA_QUERY = "SELECT type,name,tbl_name,sql FROM sqlite_schema ORDER BY type,name"


def _create_schema(connection: sqlite3.Connection) -> None:
    """현재 앱이 정의한 테이블과 인덱스만 생성한다."""
    for statement in (*TABLES, *INDEXES):
        connection.execute(statement)


def build_portable_database(snapshot: Path, target: Path) -> None:
    """일관된 사본의 업무 행만 새 파일로 옮겨 삭제된 비밀값의 잔여 페이지도 제외한다."""
    target.touch(mode=0o600, exist_ok=False)
    with closing(sqlite3.connect(target.as_uri() + "?mode=rw", uri=True)) as connection:
        _create_schema(connection)
        connection.execute("ATTACH DATABASE ? AS original", (snapshot.as_uri() + "?mode=ro",))
        # 허용 목록은 전달받은 파일이 아니라 현재 앱이 생성한 스키마에서 구한다.
        tables = [
            row[0]
            for row in connection.execute(
                "SELECT name FROM main.sqlite_schema WHERE type='table' ORDER BY name"
            )
        ]
        actual = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM original.sqlite_schema WHERE type='table' AND name NOT GLOB 'sqlite_stat*'"
            )
        }
        if set(tables) != actual:
            raise ValueError("백업할 DB의 테이블 구성이 현재 앱과 다릅니다.")
        for table in tables:
            if table in _DEVICE_TABLES or table == "sqlite_sequence":
                continue
            columns = [row[1] for row in connection.execute(f'PRAGMA main.table_info("{table}")')]
            original = {
                row[1] for row in connection.execute(f'PRAGMA original.table_xinfo("{table}")')
            }
            if set(columns) != original:
                raise ValueError("백업할 DB의 열 구성이 현재 앱과 다릅니다.")
            # 과거 마이그레이션으로 열 순서가 달라진 DB도 열 이름을 기준으로 옮긴다.
            fields = ",".join('"' + column + '"' for column in columns)
            connection.execute(
                f'INSERT INTO main."{table}"({fields}) SELECT {fields} FROM original."{table}"'
            )
        # 마지막 실행 기록이 삭제됐어도 번호가 재사용되지 않도록 증가값을 보존한다.
        connection.execute("DELETE FROM main.sqlite_sequence")
        connection.execute(
            "INSERT INTO main.sqlite_sequence(name,seq) SELECT name,seq FROM original.sqlite_sequence"
        )
        connection.commit()
    validate_portable_database(target)


def validate_portable_database(path: Path) -> None:
    """허용된 스키마와 버전·제약·설정·기기 권한 부재를 읽기 전용 연결에서 검사한다."""
    with (
        closing(sqlite3.connect(":memory:")) as reference,
        closing(sqlite3.connect(path.as_uri() + "?mode=ro&immutable=1", uri=True)) as source,
    ):
        source.execute("PRAGMA trusted_schema=OFF")
        _create_schema(reference)
        # 원본 DDL을 실행하지 않고 표준 사본과 대조한 뒤에만 업무 테이블에 접근한다.
        expected = reference.execute(_SCHEMA_QUERY).fetchall()
        if source.execute(_SCHEMA_QUERY).fetchmany(len(expected) + 1) != expected:
            raise ValueError("현재 앱에서 불러올 수 없는 DB 스키마입니다.")
        versions = source.execute("SELECT version FROM schema_versions ORDER BY version").fetchmany(
            SCHEMA_VERSION + 1
        )
        if versions != [(version,) for version in range(1, SCHEMA_VERSION + 1)]:
            raise ValueError("현재 앱에서 불러올 수 없는 DB 버전입니다.")
        if (
            source.execute("PRAGMA integrity_check(1)").fetchone() != ("ok",)
            or source.execute("PRAGMA foreign_key_check").fetchone() is not None
        ):
            raise ValueError("백업 DB의 무결성을 확인하지 못했습니다.")
        if any(
            source.execute(f"SELECT 1 FROM {table} LIMIT 1").fetchone() for table in _DEVICE_TABLES
        ):
            raise ValueError("기기 접근 권한을 포함한 백업은 불러올 수 없습니다.")
        source.row_factory = sqlite3.Row
        if read_document(source) is None:
            raise ValueError("서버를 시작하는 데 필요한 업무 설정이 백업에 없습니다.")
        read_settings(source)


def preserve_device_access(current: Path, staged: Path) -> None:
    """새 업무 사본에 현재 기기의 접근 허용 여부와 토큰 폐기 이력을 함께 이식한다."""
    with closing(sqlite3.connect(staged.as_uri() + "?mode=rw", uri=True)) as connection:
        connection.execute("ATTACH DATABASE ? AS device", (current.as_uri() + "?mode=ro",))
        with connection:
            for table in _DEVICE_TABLES:
                connection.execute(f"DELETE FROM main.{table}")
                connection.execute(f"INSERT INTO main.{table} SELECT * FROM device.{table}")
