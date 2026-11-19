"""새 파일에만 SQLite 사본을 만들고 완성 여부를 검증한다."""

import os
import sqlite3
from contextlib import closing
from pathlib import Path


def discard_database_copy(path: Path) -> None:
    """호출자가 방금 만든 사본과 SQLite가 남긴 부속 파일을 함께 정리한다."""
    for suffix in ("-journal", "-wal", "-shm", ""):
        Path(str(path) + suffix).unlink(missing_ok=True)


def copy_database(source: Path, destination: Path) -> None:
    """읽기 전용 원본에서 WAL까지 반영한 권한 600의 검증된 사본을 만든다."""
    # 독점 생성에 성공한 경우에만 실패한 목적지를 정리할 수 있다.
    descriptor = os.open(destination, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        os.close(descriptor)
        with (
            closing(sqlite3.connect(source.as_uri() + "?mode=ro", uri=True)) as original,
            closing(sqlite3.connect(destination.as_uri() + "?mode=rw", uri=True)) as copied,
        ):
            original.backup(copied)
            if copied.execute("PRAGMA quick_check").fetchone() != ("ok",):
                raise sqlite3.DatabaseError("DB 사본의 무결성을 확인하지 못했습니다.")
    except BaseException:
        discard_database_copy(destination)
        raise
