"""앱이 직접 생성한 사본만 생성 순서대로 기록하고 성공한 작업 뒤에 정리한다."""

import logging
import os
import re
import stat
from pathlib import Path
from typing import Literal

from src.infrastructure.private_files import read_private_file, write_private_file

BackupKind = Literal["pre-install", "pre-restore"]
_SUFFIX = {"pre-install": ".sqlite3", "pre-restore": ".maengbackup"}
_KEEP = 3


def _identity(path: Path) -> str | None:
    """파일 교체·수정·링크를 감지해 사용자가 바꾼 파일은 관리에서 제외한다."""
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1:
        return None
    return f"{info.st_dev}-{info.st_ino}-{info.st_ctime_ns}-{info.st_size}"


def _records(home: Path, kind: BackupKind) -> tuple[Path, list[str]]:
    """종류별 기록 전체의 형식을 확인한 뒤에만 파일 이름으로 사용한다."""
    if (home / "backups").is_symlink():
        raise ValueError("백업 폴더의 실제 경로를 확인하세요.")
    ledger = home / f".{kind}-copies"
    try:
        lines = read_private_file(ledger, max_bytes=1024 * 1024).decode("ascii").splitlines()
    except FileNotFoundError:
        lines = []
    pattern = re.compile(
        rf"{kind}-[0-9]{{8}}T[0-9]{{12}}Z{re.escape(_SUFFIX[kind])} [0-9]+(?:-[0-9]+){{3}}"
    )
    if len(lines) != len(set(lines)) or not all(pattern.fullmatch(line) for line in lines):
        raise ValueError("자동 백업 기록을 확인하세요.")
    return ledger, lines


def register_copy(path: Path, kind: BackupKind) -> None:
    """생성이 끝난 사본을 등록하고 시계가 역행해도 생성 순서를 유지한다."""
    try:
        ledger, lines = _records(path.parent.parent, kind)
        identity = _identity(path)
        if identity is None:
            raise ValueError("자동 백업 파일을 확인하세요.")
        lines.append(f"{path.name} {identity}")
        write_private_file(ledger, "\n".join(lines).encode("ascii"))
    except (OSError, ValueError):
        # 보관 기록만 손상된 경우에도 완성된 복구 지점은 그대로 사용할 수 있다.
        logging.getLogger(__name__).warning("자동 백업을 정리 대상으로 등록하지 못했습니다.")


def prune_copies(home: Path, kind: BackupKind) -> None:
    """성공 확정 후 같은 종류의 최근 세 사본을 보존하고 정리 오류는 재시도한다."""
    try:
        ledger, lines = _records(home, kind)
        kept: list[str] = []
        for line in reversed(lines):
            name, identity = line.split()
            path = home / "backups" / name
            # 경로가 같아도 사용자가 교체한 파일이나 수동 반출 사본은 삭제하지 않는다.
            if _identity(path) != identity:
                continue
            if len(kept) < _KEEP:
                kept.append(line)
            else:
                path.unlink()
        if list(reversed(kept)) != lines:
            write_private_file(ledger, "\n".join(reversed(kept)).encode("ascii"))
    except (OSError, ValueError):
        # 서비스와 데이터는 이미 확정되었으므로 다음 성공 시 남은 기록부터 정리한다.
        logging.getLogger(__name__).warning("자동 백업 정리를 완료하지 못했습니다.")
