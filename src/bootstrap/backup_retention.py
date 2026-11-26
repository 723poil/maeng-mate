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


def _move_exclusive(directory: int, source: str, destination: int, target: str) -> None:
    """열린 macOS 디렉터리 안에서 목적지가 비어 있을 때만 이름을 옮긴다."""
    if sys.platform != "darwin":
        raise OSError("독점 이름 변경을 지원하지 않습니다.")
    rename = ctypes.CDLL(None, use_errno=True).renameatx_np
    rename.argtypes = (ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint)
    rename.restype = ctypes.c_int
    if rename(directory, os.fsencode(source), destination, os.fsencode(target), 0x00000004):
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error))


@contextmanager
def _open_directory(path: Path) -> Iterator[int]:
    """열어 둔 디렉터리 안에서만 후속 항목 이동과 제거를 수행한다."""
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        yield descriptor
    finally:
        os.close(descriptor)


def _birthtime(info: os.stat_result) -> str:
    """macOS의 파일 생성 시각을 확인할 수 있을 때만 정리한다."""
    value: object = getattr(info, "st_birthtime", None)
    if not isinstance(value, float):
        raise ValueError("파일 생성 시각을 확인할 수 없습니다.")
    return value.hex()


def _finish_claim(directory: int, queue: int, entry: str, lines: list[str]) -> None:
    """등록 기록과 이동된 항목을 대조해 재시작 후에도 같은 정리를 완료한다."""
    fields = entry.split(" ")
    if len(fields) != 4 or " ".join(fields[:2]) not in lines:
        raise ValueError("자동 백업 정리 폴더에 확인할 수 없는 항목이 있습니다.")
    name, identity, birth, modified = fields
    if not re.fullmatch(r"-?0x[01]\.[0-9a-f]{1,13}p[+-][0-9]{1,4}", birth) or not re.fullmatch(
        r"-?[0-9]{1,20}", modified
    ):
        raise ValueError("자동 백업 이동 기록을 확인하세요.")
    device, inode, _, size = identity.split("-")
    claimed = os.stat(entry, dir_fd=queue, follow_symlinks=False)
    if (
        stat.S_ISREG(claimed.st_mode)
        and claimed.st_uid == os.getuid()
        and str(claimed.st_dev) == device
        and str(claimed.st_ino) == inode
        and str(claimed.st_size) == size
        and _birthtime(claimed) == birth
        and str(claimed.st_mtime_ns) == modified
    ):
        # 잠금으로 직렬화한 앱 전용 폴더의 링크 하나만 제거한다.
        # 다른 이름으로 보존된 같은 inode의 내용과 공개 경로는 건드리지 않는다.
        os.unlink(entry, dir_fd=queue)
    else:
        # 교체 항목은 공개 이름이 비어 있을 때만 되돌린다. 충돌하면 다음 성공 때 재시도한다.
        _move_exclusive(queue, entry, directory, name)
        os.fsync(directory)
    os.fsync(queue)


def _prune(home: Path, kind: BackupKind, directory: int, lines: list[str]) -> None:
    """종류별 단일 대기 항목을 먼저 처리하고 새 만료 사본을 한 개씩 이동한다."""
    pending = home / f".{kind}-retiring"
    pending.mkdir(mode=0o700, exist_ok=True)
    # 이동 전에 대기 폴더 이름까지 확정해야 중단 후 같은 복구 기록을 찾을 수 있다.
    with _open_directory(home) as parent:
        os.fsync(parent)
    with _open_directory(pending) as queue:
        info = os.fstat(queue)
        if info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise ValueError("자동 백업 정리 폴더의 권한을 확인하세요.")
        entries = os.listdir(queue)
        if len(entries) > 1:
            raise ValueError("자동 백업 정리 폴더의 항목을 확인하세요.")
        if entries:
            _finish_claim(directory, queue, entries[0], lines)

        kept: list[str] = []
        for line in reversed(lines):
            name, identity = line.split()
            try:
                before = os.stat(name, dir_fd=directory, follow_symlinks=False)
            except FileNotFoundError:
                continue
            if _identity(before) != identity:
                continue
            if len(kept) < _KEEP:
                kept.append(line)
                continue
            # 이동 자체가 복구 기록이다. 원래 이름과 생성 기록을 함께 옮겨 별도 표식과
            # 이동 사이에 생기는 중단 구간을 없앤다. 공개 이름에는 unlink를 호출하지 않는다.
            entry = f"{line} {_birthtime(before)} {before.st_mtime_ns}"
            _move_exclusive(directory, name, queue, entry)
            os.fsync(queue)
            os.fsync(directory)
            _finish_claim(directory, queue, entry, lines)
        lines[:] = reversed(kept)


def prune_copies(home: Path, kind: BackupKind) -> None:
    """성공 확정 후 최근 세 사본을 보존하고 중단된 정리는 다음 성공 때 이어간다."""
    try:
        with (
            _records(home, kind, create=False) as lines,
            _open_directory(home / "backups") as directory,
        ):
            _prune(home, kind, directory, lines)
    except FileNotFoundError:
        return
    except (OSError, ValueError):
        _LOG.warning("자동 백업 정리를 완료하지 못해 자료를 보존했습니다.")
