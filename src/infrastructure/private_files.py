"""로컬 실행 상태와 등록 파일을 안전하게 교체한다."""

import os
import tempfile
from pathlib import Path


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
