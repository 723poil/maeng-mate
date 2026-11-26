"""서비스와 저장 잠금을 보유한 관리자가 전체 복원을 확정하거나 되돌린다."""

import os
import sqlite3
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory

from src.bootstrap.backup_retention import register_copy
from src.bootstrap.environment import ConfigError
from src.infrastructure.data_backup import create_backup, preserve_device_access
from src.infrastructure.private_files import read_private_file, write_private_file
from src.infrastructure.sqlite.backup import copy_database
from src.infrastructure.work_rules_file import MAX_WORK_RULES_BYTES

_PENDING = ".restore-pending"
_DATABASE = "data.sqlite3"
_RULES = "work-rules.json"


def _sync(path: Path) -> None:
    """다음 단계의 표식을 쓰기 전에 파일 내용 또는 디렉터리 변경을 확정한다."""
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _rules_bytes(path: Path) -> bytes:
    """규칙 부재는 빈 바이트로 표시하고 실제 규칙 파일은 비어 있지 않아야 한다."""
    try:
        data = read_private_file(path, max_bytes=MAX_WORK_RULES_BYTES)
    except FileNotFoundError:
        return b""
    if not data:
        raise ConfigError("근무 규칙 파일이 비어 있습니다.")
    return data


def _install(database: Path, source: Path, rules: Path, rules_bytes: bytes) -> None:
    """SQLite가 저널과 WAL을 관리하는 쓰기 연결로 교체하고 규칙 파일을 확정한다."""
    # 데이터베이스 위치는 지원 디렉터리와 다른 볼륨일 수 있다. 열린 연결에 쓰면
    # 단순 파일 교체 후 예전 WAL이 새 DB에 재생되는 문제도 피할 수 있다.
    with (
        closing(sqlite3.connect(source.as_uri() + "?mode=ro", uri=True)) as original,
        closing(sqlite3.connect(database.as_uri() + "?mode=rw", uri=True)) as live,
    ):
        live.execute("PRAGMA synchronous=FULL")
        # WAL에서는 페이지 크기를 변경할 수 없어 다른 크기의 사본은 롤백 저널로 받는다.
        if (
            original.execute("PRAGMA page_size").fetchone()
            != live.execute("PRAGMA page_size").fetchone()
        ):
            live.execute("PRAGMA journal_mode=DELETE")
        original.backup(live)
        live.execute("PRAGMA journal_mode=WAL")
    _sync(database.parent)
    if rules_bytes:
        write_private_file(rules, rules_bytes)
        _sync(rules)
    else:
        rules.unlink(missing_ok=True)
    _sync(rules.parent)


def pending_restore(home: Path) -> bool:
    """서비스가 시작 전에 완료 또는 복구를 기다려야 할 교체가 있는지 확인한다."""
    pending = home / _PENDING
    if pending.is_symlink() or (pending.exists() and not pending.is_dir()):
        raise ConfigError("복원 작업 디렉터리를 확인하세요.")
    return pending.is_dir()


def begin_restore(home: Path, database: Path, staged: Path) -> None:
    """이동용 백업과 기기 인증을 포함한 복구 원본을 확정한 뒤 새 데이터를 적용한다."""
    if pending_restore(home):
        raise ConfigError("이전 복원 작업을 먼저 복구하세요.")
    rules = home / _RULES
    backups = home / "backups"
    if backups.is_symlink():
        raise ConfigError("백업 폴더의 실제 경로를 확인하세요.")
    backups.mkdir(mode=0o700, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    recovery = backups / f"pre-restore-{stamp}.maengbackup"

    def register_recovery(descriptor: int, before: os.stat_result) -> None:
        """복구 파일과 폴더의 동기화를 마치고 생성 핸들의 식별값을 남긴다."""
        os.fsync(descriptor)
        _sync(backups)
        register_copy(recovery, "pre-restore", before, os.fstat(descriptor))

    create_backup(database, rules, recovery, on_created=register_recovery)

    new_rules = _rules_bytes(staged / _RULES)
    preserve_device_access(database, staged / _DATABASE)
    with TemporaryDirectory(prefix=".restore-prepare-", dir=home) as temporary:
        undo = Path(temporary)
        # 현재 권한은 이동용 백업에서 빠지므로 로컬 복구용 DB를 별도로 보존한다.
        copy_database(database, undo / _DATABASE)
        write_private_file(undo / _RULES, _rules_bytes(rules))
        write_private_file(undo / "target", os.fsencode(database.resolve()))
        for name in (_DATABASE, _RULES, "target"):
            _sync(undo / name)
        _sync(undo)
        # 두 파일을 모두 되돌릴 원본이 있을 때에만 실행 중 표시를 게시한다.
        undo.rename(home / _PENDING)
        _sync(home)
    _install(database, staged / _DATABASE, rules, new_rules)


def finish_restore(home: Path) -> None:
    """복원 확정과 임시 자료 정리를 분리해 정리 도중 종료돼도 완료로 인식한다."""
    if pending_restore(home):
        with TemporaryDirectory(
            prefix=".restore-finished-", dir=home, ignore_cleanup_errors=True
        ) as temporary:
            (home / _PENDING).rename(Path(temporary) / "original")
            _sync(home)


def rollback_restore(home: Path, database: Path) -> None:
    """복구 원본을 소비하지 않고 다시 적용해 중단된 복구도 같은 경로로 재시도한다."""
    if not pending_restore(home):
        return
    undo = home / _PENDING
    if read_private_file(undo / "target", max_bytes=16384) != os.fsencode(database.resolve()):
        raise ConfigError("복원 전 데이터베이스의 저장 위치를 확인하세요.")
    rules = read_private_file(undo / _RULES, max_bytes=MAX_WORK_RULES_BYTES)
    _install(database, undo / _DATABASE, home / _RULES, rules)
    finish_restore(home)
