"""생성자가 확인한 사본만 기록하고 성공한 작업 뒤에 보관 용량을 회수한다."""

import ctypes
import logging
import os
import re
import stat
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Literal

BackupKind = Literal["pre-install", "pre-restore"]
_SUFFIX = {"pre-install": ".sqlite3", "pre-restore": ".maengbackup"}
_KEEP = 3
_HEADER = b"Catch automatic backups v2\n"
_LOG = logging.getLogger(__name__)


def _identity(info: os.stat_result) -> str | None:
    """생성자가 연 파일과 자료·권한·링크 상태가 같은 사본만 식별한다."""
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1:
        return None
    return f"{info.st_dev}-{info.st_ino}-{info.st_ctime_ns}-{info.st_size}"


@contextmanager
def _records(home: Path, kind: BackupKind, *, create: bool) -> Iterator[list[str]]:
    """서비스 잠금 안에서 확인한 기록 파일의 열린 핸들만 수정한다."""
    ledger = home / f".{kind}-copies-v2"
    flags = os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK
    fresh = False
    if create:
        try:
            fd = os.open(ledger, flags | os.O_CREAT | os.O_EXCL, 0o600)
            fresh = True
        except FileExistsError:
            fd = os.open(ledger, flags)
    else:
        fd = os.open(ledger, flags)
    with os.fdopen(fd, "r+b") as stream:
        info = os.fstat(fd)
        if _identity(info) is None or info.st_mode & 0o077:
            raise ValueError("자동 백업 기록의 권한을 확인하세요.")
        original = stream.read(1024 * 1024 + 1)
        if not fresh and (not original.startswith(_HEADER) or len(original) > 1024 * 1024):
            raise ValueError("자동 백업 기록을 확인하세요.")
        lines = original[len(_HEADER) :].decode("ascii").splitlines()
        pattern = re.compile(
            rf"{kind}-[0-9]{{8}}T[0-9]{{12}}Z{re.escape(_SUFFIX[kind])} [0-9]+(?:-[0-9]+){{3}}"
        )
        if len(lines) != len(set(lines)) or not all(pattern.fullmatch(line) for line in lines):
            raise ValueError("자동 백업 기록을 확인하세요.")
        yield lines
        updated = _HEADER + "\n".join(lines).encode("ascii")
        if updated != original:
            stream.seek(0)
            stream.write(updated)
            stream.truncate()
            stream.flush()
            os.fsync(fd)


def register_copy(
    path: Path, kind: BackupKind, before: os.stat_result, published: os.stat_result
) -> None:
    """공개된 이름을 다시 조회하지 않고 생성 핸들의 식별값을 기록한다."""
    try:
        identity = _identity(published)
        if (
            identity is None
            or not os.path.samestat(before, published)
            or before.st_size != published.st_size
            or before.st_mtime_ns != published.st_mtime_ns
        ):
            raise ValueError("자동 백업 파일을 확인하세요.")
        with _records(path.parent.parent, kind, create=True) as lines:
            lines.append(f"{path.name} {identity}")
    except (OSError, ValueError):
        _LOG.warning("자동 백업을 정리 대상으로 등록하지 못했습니다.")


def _move_exclusive(directory: int, source: str, target: str) -> None:
    """열린 macOS 디렉터리 안에서 목적지가 비어 있을 때만 이름을 옮긴다."""
    if sys.platform != "darwin":
        raise OSError("독점 이름 변경을 지원하지 않습니다.")
    rename = ctypes.CDLL(None, use_errno=True).renameatx_np
    rename.argtypes = (ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint)
    rename.restype = ctypes.c_int
    if rename(directory, os.fsencode(source), directory, os.fsencode(target), 0x00000004):
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error))


def _retire(directory: int, name: str, identity: str) -> None:
    """이름을 독점 이동한 뒤 처음 연 파일의 핸들로만 백업 내용을 비운다."""
    fd = os.open(name, os.O_RDWR | os.O_NONBLOCK | os.O_NOFOLLOW, dir_fd=directory)
    try:
        before = os.fstat(fd)
        if _identity(before) != identity:
            return
        retired = f".retired-{name}.entry"
        _move_exclusive(directory, name, retired)
        claimed = os.stat(retired, dir_fd=directory, follow_symlinks=False)
        # 이름 변경은 ctime을 바꾸므로 열린 원본의 inode와 자료 수정 시각을 대조한다.
        if (
            not os.path.samestat(before, claimed)
            or claimed.st_nlink != 1
            or claimed.st_mtime_ns != before.st_mtime_ns
            or claimed.st_size != before.st_size
        ):
            # 원래 이름도 다시 사용됐으면 양쪽을 보존하고 사용자에게 충돌을 알린다.
            try:
                _move_exclusive(directory, retired, name)
            except OSError:
                _LOG.warning("이름이 겹친 파일을 backups의 .retired- 이름으로 보존했습니다.")
                raise
            return
        # 이름으로 삭제하면 마지막 순간의 교체 파일까지 지울 수 있다. 열린 사본만
        # 비우고 숨김 항목을 남겨 같은 이름에 새로 놓인 파일에는 쓰지 않는다.
        os.ftruncate(fd, 0)
    finally:
        os.close(fd)


def prune_copies(home: Path, kind: BackupKind) -> None:
    """성공 확정 후 같은 종류의 최근 세 사본을 보존하고 오류 때는 자료를 남긴다."""
    try:
        with _records(home, kind, create=False) as lines:
            directory = os.open(home / "backups", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                kept: list[str] = []
                recent = 0
                for line in reversed(lines):
                    name, identity = line.split()
                    try:
                        info = os.stat(name, dir_fd=directory, follow_symlinks=False)
                    except FileNotFoundError:
                        continue
                    if _identity(info) != identity:
                        continue
                    if recent < _KEEP:
                        recent += 1
                        kept.append(line)
                        continue
                    try:
                        _retire(directory, name, identity)
                    except OSError:
                        # 한 사본의 이름 충돌 때문에 다른 사본의 용량 회수가 막히지 않는다.
                        kept.append(line)
                        _LOG.warning("자동 백업 하나를 정리하지 못해 보존했습니다.")
                lines[:] = reversed(kept)
            finally:
                os.close(directory)
    except FileNotFoundError:
        return
    except (OSError, ValueError):
        _LOG.warning("자동 백업 정리를 완료하지 못했습니다.")
