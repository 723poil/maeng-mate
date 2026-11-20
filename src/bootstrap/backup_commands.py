"""설정 화면의 파일 선택을 로컬 백업과 독립 복원 프로세스에 연결한다."""

import hashlib
import json
import os
import sqlite3
import subprocess
from pathlib import Path
from tempfile import TemporaryDirectory

from src.application.data_backup import BackupPreview, DataBackupError
from src.application.runtime_settings_service import RestartOperation
from src.bootstrap.app_settings import data_environment
from src.bootstrap.environment import ConfigError
from src.bootstrap.executable import executable_command, execution_environment
from src.bootstrap.instance import application_directory, coordination_lock, project_root
from src.infrastructure.data_backup import create_backup, unpack_backup


def export_backup(path: str) -> None:
    """온라인 DB와 근무 규칙을 사용자가 고른 새 파일로 저장한다."""
    from src.bootstrap.backup_transaction import pending_restore

    home = application_directory()
    try:
        with coordination_lock("service", shared=True), coordination_lock("storage", shared=True):
            if pending_restore(home):
                raise DataBackupError("진행 중인 복원을 마친 뒤 다시 시도하세요.")
            database = Path(data_environment({})["DATABASE_PATH"])
            # application에서 받은 경로 값은 파일을 다루는 시작 경계에서만 해석한다.
            create_backup(database, home / "work-rules.json", Path(path))
    except FileExistsError:
        raise DataBackupError("이미 있는 파일입니다. 새 이름을 선택하세요.") from None
    except (ValueError, OSError, ConfigError, sqlite3.Error):
        raise DataBackupError(
            "백업을 저장하지 못했습니다. 저장 위치와 앱 상태를 확인하세요."
        ) from None


def stage_archive(path: Path, directory: Path) -> BackupPreview:
    """검증한 DB와 규칙의 지문을 확인 대화상자와 실제 반입에 함께 사용한다."""
    staged = directory / "contents"
    staged.mkdir(mode=0o700)
    stamp = unpack_backup(path, staged)
    with (staged / "data.sqlite3").open("rb") as database:
        digest = hashlib.sha256(hashlib.file_digest(database, "sha256").digest())
    # DB와 규칙별 고정 길이 해시를 결합해 규칙 파일의 부재도 구분한다.
    rules = staged / "work-rules.json"
    if rules.exists():
        digest.update(hashlib.sha256(rules.read_bytes()).digest())
    return BackupPreview(stamp, digest.hexdigest())


def preview_backup(path: str) -> BackupPreview:
    """서비스가 실행 중인 동안 선택한 파일의 내용과 생성 시각을 확인한다."""
    try:
        with TemporaryDirectory() as temporary:
            return stage_archive(Path(path), Path(temporary))
    except (OSError, ValueError, sqlite3.Error):
        raise DataBackupError(
            "백업 파일을 읽을 수 없습니다. 파일 형식과 앱 버전을 확인하세요."
        ) from None


def launch_restore(path: str, preview: BackupPreview) -> RestartOperation:
    """메뉴바가 종료되어도 살아 있는 복원 도우미에 확인한 파일을 넘긴다."""
    # 파일 경로와 내부 자료를 launchd 인수나 로그에 남기지 않는다.
    request = json.dumps({"file": path, "stamp": preview.stamp, "digest": preview.digest})
    try:
        process = subprocess.Popen(
            executable_command("restore", "apply"),
            cwd=project_root(),
            env=execution_environment(os.environ),
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        assert process.stdin is not None
        with process.stdin:
            process.stdin.write(request.encode())
        return process
    except OSError:
        raise DataBackupError("복원 도우미를 시작하지 못했습니다.") from None
