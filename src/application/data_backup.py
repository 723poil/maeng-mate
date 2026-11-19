"""설정 화면에서 호출하는 로컬 백업·전체 복원 계약이다."""

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from src.application.runtime_settings_service import RestartOperation


class DataBackupError(Exception):
    """경로나 원문 없이 사용자에게 전달할 백업 작업 오류다."""


@dataclass(frozen=True)
class BackupPreview:
    """사용자가 확인한 생성 시각과 해당 파일의 내용 식별값이다."""

    stamp: str
    digest: str


@dataclass(frozen=True)
class DataBackupActions:
    """파일 선택과 서비스 전환 구현을 분리하는 세 가지 사용자 동작이다."""

    export: Callable[[Path], None]
    preview: Callable[[Path], BackupPreview]
    restore: Callable[[Path, BackupPreview], RestartOperation]
