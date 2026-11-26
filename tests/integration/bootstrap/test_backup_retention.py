"""임시 앱 디렉터리에서 설치·복원 사본의 확정 경계와 수동 파일 보호를 확인한다."""

import sqlite3
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

import pytest

from src.bootstrap import backup_restore, packaged_app
from src.bootstrap.backup_commands import preview_backup, stage_archive
from src.bootstrap.backup_transaction import begin_restore, pending_restore
from src.infrastructure.data_backup import create_backup


def test_install_retention(tmp_path: Path) -> None:
    """반복 설치와 실패 뒤 재시도에서 등록된 기존 DB 사본만 세 개로 줄인다."""
    database = tmp_path / "data.sqlite3"
    values = {"DATABASE_PATH": str(database)}
    packaged_app.initialize_database(str(database))
    resources = tmp_path / "Resources"
    resources.mkdir()
    (resources / "build-info.json").write_text("{}")
    with (
        patch.object(packaged_app, "application_directory", return_value=tmp_path),
        patch.object(packaged_app, "bundle_contents", return_value=tmp_path),
    ):
        for _ in range(4):
            packaged_app.prepare_state(values)
        backups = tmp_path / "backups"
        copies = sorted(backups.glob("*.sqlite3"))
        assert len(copies) == 4
        # 같은 이름에 새 파일을 놓아도 원래 생성 기록으로 지워서는 안 된다.
        copies[0].unlink()
        copies[0].write_bytes(b"replacement")
        packaged_app.prepare_state(values)
        (resources / "build-info.json").unlink()
        with pytest.raises(FileNotFoundError):
            packaged_app.record_installation()
        assert len(list(backups.glob("*.sqlite3"))) == 5
        (resources / "build-info.json").write_text("{}")
        packaged_app.record_installation()
        assert copies[0].read_bytes() == b"replacement"
        assert not copies[1].exists()
        assert len(list(backups.glob("*.sqlite3"))) == 4


def test_restore_retention(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """실제 백업·교체·재시작 복구 뒤 다음 성공 시에만 초과 사본을 정리한다."""
    monkeypatch.setenv("HOME", str(tmp_path))
    database = tmp_path / "data.sqlite3"
    values = {"DATABASE_PATH": str(database)}
    packaged_app.initialize_database(str(database))
    backups = tmp_path / "backups"
    backups.mkdir()
    manual = backups / "pre-restore-20000101T000000000000Z.maengbackup"
    create_backup(database, tmp_path / "work-rules.json", manual)
    preview = preview_backup(str(manual))
    unknown = backups / "unknown"
    unknown.write_text("keep")
    with (
        patch.object(backup_restore, "application_directory", return_value=tmp_path),
        patch.object(backup_restore, "_manager") as manager,
        patch.object(backup_restore, "preflight", return_value=values),
        patch.object(backup_restore, "data_environment", return_value=values),
        patch.object(backup_restore, "_stop_for_restore"),
        patch.object(backup_restore, "start_apps"),
        patch.object(backup_restore, "wait_ready"),
    ):
        manager.return_value.path.return_value = manual
        for _ in range(4):
            backup_restore.restore_selected(manual, preview)
        assert len(list(backups.glob("*.maengbackup"))) == 4
        stage_archive(manual, tmp_path)
        # 설정 행의 유무로 교체와 재시작 롤백이 실제 DB를 되돌리는지 확인한다.
        with closing(sqlite3.connect(database)) as connection:
            connection.execute("DELETE FROM runtime_settings")
            connection.commit()
        begin_restore(tmp_path, database, tmp_path / "contents")
        with closing(sqlite3.connect(database)) as connection:
            assert connection.execute("SELECT count(*) FROM runtime_settings").fetchone() == (1,)
        assert pending_restore(tmp_path)
        assert len(list(backups.glob("*.maengbackup"))) == 5
        backup_restore.recover_before_start(manager.return_value, tmp_path)
        assert not pending_restore(tmp_path)
        with closing(sqlite3.connect(database)) as connection:
            assert connection.execute("SELECT count(*) FROM runtime_settings").fetchone() == (0,)
        assert len(list(backups.glob("*.maengbackup"))) == 5
        backup_restore.restore_selected(manual, preview)
        assert len(list(backups.glob("*.maengbackup"))) == 4
        assert preview_backup(str(manual)) == preview
        assert unknown.read_text() == "keep"
