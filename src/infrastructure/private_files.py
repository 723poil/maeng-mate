"""로컬 실행 상태와 등록 파일을 안전하게 교체한다."""

import os
import stat
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path


@contextmanager
def _private_descriptor(path: Path, flags: int) -> Iterator[int]:
    """열린 파일의 종류·소유자·권한을 검사하고 실패 때도 파일을 닫는다."""
    descriptor = os.open(path, flags | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise ValueError("설정 파일의 종류와 권한을 확인하세요.")
        yield descriptor
    finally:
        os.close(descriptor)


def read_private_file(path: Path, *, max_bytes: int) -> bytes:
    """읽기로 파일을 만들지 않고 허용 크기를 넘은 자료는 거부한다."""
    with _private_descriptor(path, os.O_RDONLY) as descriptor:
        with os.fdopen(descriptor, "rb", closefd=False) as stream:
            data = stream.read(max_bytes + 1)
    if len(data) > max_bytes:
        raise ValueError("설정 파일의 크기를 확인하세요.")
    return data


@contextmanager
def private_file_lock(path: Path) -> Iterator[None]:
    """모든 작성자가 같은 잠금 파일을 열어 버전 확인과 교체를 직렬화한다."""
    import fcntl

    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with _private_descriptor(path, os.O_CREAT | os.O_RDWR) as descriptor:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        # 파일을 남겨 대기 중인 작성자와 새 작성자가 같은 inode의 잠금을 사용한다.
        yield


def write_private_file(path: Path, content: bytes) -> None:
    """같은 디렉터리에서 권한600의 완성된 파일만 원자적으로 교체한다."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor, temporary = tempfile.mkstemp(prefix="." + path.name + "-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        if Path(temporary).exists():
            Path(temporary).unlink()
