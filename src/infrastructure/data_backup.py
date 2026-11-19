"""앱 데이터와 선택적인 근무 규칙을 한 개의 개인 백업 파일로 저장하고 검증한다."""

import ctypes
import errno
import json
import os
import shutil
import stat
import sys
import tempfile
from contextlib import ExitStack
from datetime import UTC, datetime, timedelta
from io import BufferedReader, FileIO
from pathlib import Path
from zipfile import ZIP_STORED, BadZipFile, ZipFile

from src.application.work_rules_settings import document_payload
from src.infrastructure.private_files import private_file_lock, write_private_file
from src.infrastructure.sqlite.backup import copy_database
from src.infrastructure.sqlite.portable_backup import (
    build_portable_database,
    preserve_device_access,
    validate_portable_database,
)
from src.infrastructure.work_rules_file import MAX_WORK_RULES_BYTES, WorkRulesFile

__all__ = ["MAX_ARCHIVE_BYTES", "create_backup", "preserve_device_access", "unpack_backup"]

_DATABASE = "data.sqlite3"
_RULES = "work-rules.json"
_MANIFEST = "manifest.json"
_MAX_DB_BYTES = 8 * 1024**3
MAX_ARCHIVE_BYTES = _MAX_DB_BYTES + 1024**2
_MEMBER_LIMITS = {_DATABASE: _MAX_DB_BYTES, _RULES: MAX_WORK_RULES_BYTES, _MANIFEST: 1024}
_CHUNK_BYTES = 1024**2


class _ArchiveReader(BufferedReader):
    """항목 목록의 위조된 길이가 파일 전체를 한 번에 메모리로 읽지 못하게 한다."""

    def read(self, size: int | None = -1, /) -> bytes:
        """정상 목록과 DB 복사 블록이 사용하는 크기 안에서만 읽는다."""
        if size is None or not 0 <= size <= _CHUNK_BYTES:
            raise ValueError("백업 파일의 항목 크기를 확인하세요.")
        return super().read(size)


def _publish_new_file(temporary: Path, target: Path) -> None:
    """저장 볼륨이 지원하는 독점 게시 연산으로 기존 백업을 보호한다."""
    if sys.platform == "darwin":
        # macOS에서는 하드 링크를 지원하지 않는 외장 볼륨에도 독점 이름 변경을 시도한다.
        rename = ctypes.CDLL(None, use_errno=True).renamex_np
        rename.argtypes = (ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint)
        rename.restype = ctypes.c_int
        rename_excl = 0x00000004
        if rename(os.fsencode(temporary), os.fsencode(target), rename_excl) == 0:
            return
        error = ctypes.get_errno()
        if error not in (errno.ENOTSUP, errno.EOPNOTSUPP, errno.ENOSYS):
            raise OSError(error, os.strerror(error))
    os.link(temporary, target)


def create_backup(database: Path, rules: Path, target: Path) -> None:
    """같은 시점의 DB·근무 규칙을 검증한 다음 완성된 파일을 새 이름으로만 게시한다."""
    if target.exists() or target.is_symlink():
        raise FileExistsError("같은 이름의 파일이 있습니다. 새 이름으로 저장하세요.")
    with tempfile.TemporaryDirectory() as temporary:
        staging = Path(temporary)
        snapshot = staging / "snapshot.sqlite3"
        portable = staging / _DATABASE
        # 근무 규칙이 바뀌지 않는 구간 안에서 WAL에 커밋된 DB 상태를 읽는다.
        with private_file_lock(rules.with_name(f".{rules.name}.lock")):
            saved_rules = WorkRulesFile(rules).read()
            copy_database(database, snapshot)
            created_at = datetime.now(UTC).isoformat()
        build_portable_database(snapshot, portable)
        if portable.stat().st_size > _MAX_DB_BYTES:
            raise ValueError("백업할 데이터 크기가 지원 범위를 넘었습니다.")
        manifest = {
            "format": "maeng-mate",
            "version": 1,
            "createdAt": created_at,
            "workRules": saved_rules is not None,
        }
        # 사용자 위치에는 검증한 휴대용 자료만 쓰고, 동시 저장 때 기존 파일을 덮지 않는다.
        with tempfile.TemporaryDirectory(dir=target.parent, prefix=".maeng-mate-") as publication:
            output = Path(publication) / "backup"
            output.touch(mode=0o600, exist_ok=False)
            with output.open("wb") as stream:
                with ZipFile(stream, "w") as archive:
                    archive.writestr(_MANIFEST, json.dumps(manifest))
                    archive.write(portable, _DATABASE)
                    if saved_rules is not None:
                        archive.writestr(
                            _RULES,
                            json.dumps(document_payload(saved_rules), ensure_ascii=False).encode(),
                        )
                stream.flush()
                os.fsync(stream.fileno())
            _publish_new_file(output, target)


def _creation_time(data: bytes, *, has_rules: bool) -> str:
    """앱 식별자·형식 버전·시각과 실제 규칙 파일의 존재 여부를 대조한다."""
    value: object = json.loads(data)
    if (
        not isinstance(value, dict)
        or set(value) != {"format", "version", "createdAt", "workRules"}
        or value["format"] != "maeng-mate"
        or type(value["version"]) is not int
        or value["version"] != 1
        or not isinstance(value["createdAt"], str)
        or value["workRules"] is not has_rules
    ):
        raise ValueError("지원하는 Maeng Mate 백업 파일을 선택하세요.")
    created = datetime.fromisoformat(value["createdAt"])
    if created.utcoffset() != timedelta(0):
        raise ValueError("백업 생성 시각을 확인하세요.")
    return created.isoformat()


def unpack_backup(source: Path, directory: Path) -> str:
    """선택한 파일의 구성·크기·형식을 검사하고 빈 임시 디렉터리에만 복원 후보를 만든다."""
    if any(directory.iterdir()):
        raise ValueError("복원 후보를 준비할 빈 폴더가 필요합니다.")
    with ExitStack() as cleanup:
        descriptor = os.open(source, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
        with _ArchiveReader(FileIO(descriptor, "rb")) as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or not 0 < info.st_size <= MAX_ARCHIVE_BYTES:
                raise ValueError("백업 파일의 종류와 크기를 확인하세요.")
            try:
                with ZipFile(stream) as archive:
                    names = archive.namelist()
                    has_rules = _RULES in names
                    if sorted(names) != sorted(
                        (_DATABASE, _MANIFEST, _RULES) if has_rules else (_DATABASE, _MANIFEST)
                    ):
                        raise ValueError("백업 파일 구성이 올바르지 않습니다.")
                    for member in archive.infolist():
                        if (
                            member.compress_type != ZIP_STORED
                            or member.flag_bits & 1
                            or member.compress_size != member.file_size
                            or not 0 < member.file_size <= _MEMBER_LIMITS[member.filename]
                        ):
                            raise ValueError("백업 파일의 저장 형식과 크기를 확인하세요.")
                    created_at = _creation_time(archive.read(_MANIFEST), has_rules=has_rules)
                    if has_rules:
                        rules = directory / _RULES
                        write_private_file(rules, archive.read(_RULES))
                        cleanup.callback(rules.unlink, missing_ok=True)
                        WorkRulesFile(rules).read()
                    database = directory / _DATABASE
                    database.touch(mode=0o600, exist_ok=False)
                    cleanup.callback(database.unlink, missing_ok=True)
                    with archive.open(_DATABASE) as archived, database.open("wb") as staged:
                        shutil.copyfileobj(archived, staged, length=_CHUNK_BYTES)
            except (BadZipFile, NotImplementedError):
                raise ValueError("손상된 백업 파일입니다. 다른 파일을 선택하세요.") from None
        validate_portable_database(database)
        # 검사를 통과한 파일만 호출자에게 넘기고 실패한 중간 파일은 모두 정리한다.
        cleanup.pop_all()
        return created_at
