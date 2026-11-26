"""임시 앱 디렉터리에서 설치·복원 사본의 확정 경계와 수동 파일 보호를 확인한다."""

import os
import sqlite3
import sys
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

import pytest

from src.bootstrap import backup_restore, backup_retention, backup_transaction, packaged_app
from src.bootstrap.backup_commands import preview_backup, stage_archive
from src.bootstrap.backup_transaction import begin_restore, finish_restore, pending_restore
from src.infrastructure.data_backup import create_backup

pytestmark = pytest.mark.skipif(
    sys.platform != "darwin", reason="macOS 독점 이름 변경으로 검증한다"
)


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
        assert next(backups.glob(".retired-*.entry")).stat().st_size == 0
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


@pytest.mark.parametrize("kind", ["pre-install", "pre-restore"])
def test_register_created_artifact(tmp_path: Path, kind: backup_retention.BackupKind) -> None:
    """공개 이름에 다른 파일이 놓여도 설치·복원 생성자는 원래 사본만 등록한다."""
    database = tmp_path / "data.sqlite3"
    packaged_app.initialize_database(str(database))
    manual = tmp_path / "manual.maengbackup"
    create_backup(database, tmp_path / "work-rules.json", manual)
    stage_archive(manual, tmp_path)
    replaced: list[Path] = []

    def replace_first(
        path: Path,
        category: backup_retention.BackupKind,
        before: os.stat_result,
        info: os.stat_result,
    ) -> None:
        """첫 게시물의 이름을 다른 내용으로 교체한 후 실제 등록을 계속한다."""
        if not replaced:
            path.rename(tmp_path / "saved-original")
            path.write_bytes(b"manual replacement")
            replaced.append(path)
        backup_retention.register_copy(path, category, before, info)

    owner = packaged_app if kind == "pre-install" else backup_transaction
    with (
        patch.object(packaged_app, "application_directory", return_value=tmp_path),
        patch.object(owner, "register_copy", side_effect=replace_first),
    ):
        for _ in range(4):
            if kind == "pre-install":
                packaged_app.backup_database(str(database))
            else:
                begin_restore(tmp_path, database, tmp_path / "contents")
                finish_restore(tmp_path)
        backup_retention.prune_copies(tmp_path, kind)
    assert replaced[0].read_bytes() == b"manual replacement"
    assert (tmp_path / "saved-original").stat().st_size > 0


@pytest.mark.parametrize("occupy_original", [False, True])
def test_replacement_during_retirement(tmp_path: Path, occupy_original: bool) -> None:
    """이동 시점의 교체 파일을 되돌리고 원래 이름까지 사용 중이면 양쪽을 보존한다."""
    database = tmp_path / "data.sqlite3"
    packaged_app.initialize_database(str(database))
    backups = tmp_path / "backups"
    with patch.object(packaged_app, "application_directory", return_value=tmp_path):
        for _ in range(4):
            packaged_app.backup_database(str(database))
    oldest = min(backups.glob("*.sqlite3"))
    moved = tmp_path / "saved-original"
    move = backup_retention._move_exclusive

    def switch_during_move(directory: int, source: str, target: str) -> None:
        """정리 대상의 공개 이름이 이동 직전에 교체되는 순서를 재현한다."""
        if source == oldest.name:
            oldest.rename(moved)
            oldest.write_bytes(b"first replacement")
        move(directory, source, target)
        if source == oldest.name and occupy_original:
            oldest.write_bytes(b"second replacement")

    with patch.object(backup_retention, "_move_exclusive", side_effect=switch_during_move):
        backup_retention.prune_copies(tmp_path, "pre-install")
    assert moved.stat().st_size > 0
    if occupy_original:
        assert oldest.read_bytes() == b"second replacement"
        assert next(backups.glob(".retired-*.entry")).read_bytes() == b"first replacement"
    else:
        assert oldest.read_bytes() == b"first replacement"
        assert not list(backups.glob(".retired-*.entry"))


def test_replacement_after_claim(tmp_path: Path) -> None:
    """내용 회수 직전에 공개 이름을 재사용해도 열린 이전 사본만 비운다."""
    database = tmp_path / "data.sqlite3"
    packaged_app.initialize_database(str(database))
    backups = tmp_path / "backups"
    with patch.object(packaged_app, "application_directory", return_value=tmp_path):
        for _ in range(4):
            packaged_app.backup_database(str(database))
    oldest = min(backups.glob("*.sqlite3"))
    truncate = os.ftruncate

    def reuse_name(descriptor: int, length: int) -> None:
        """원래 이름에 새 파일을 놓은 뒤 실제 핸들의 내용만 회수한다."""
        oldest.write_bytes(b"new document")
        retired = next(backups.glob(".retired-*.entry"))
        new_file = tmp_path / "second-document"
        new_file.write_bytes(b"retired name reused")
        new_file.replace(retired)
        truncate(descriptor, length)

    with patch.object(os, "ftruncate", side_effect=reuse_name):
        backup_retention.prune_copies(tmp_path, "pre-install")
    assert oldest.read_bytes() == b"new document"
    assert next(backups.glob(".retired-*.entry")).read_bytes() == b"retired name reused"


def test_retirement_name_in_use(tmp_path: Path) -> None:
    """숨김 이름을 이미 사용하는 파일과 해당 사본을 보존하고 다른 만료 사본을 정리한다."""
    database = tmp_path / "data.sqlite3"
    packaged_app.initialize_database(str(database))
    backups = tmp_path / "backups"
    with patch.object(packaged_app, "application_directory", return_value=tmp_path):
        for _ in range(5):
            packaged_app.backup_database(str(database))
    oldest, blocked, *recent = sorted(backups.glob("*.sqlite3"))
    occupied = backups / f".retired-{blocked.name}.entry"
    occupied.write_bytes(b"existing document")
    backup_retention.prune_copies(tmp_path, "pre-install")
    assert occupied.read_bytes() == b"existing document"
    assert blocked.stat().st_size > 0
    assert not oldest.exists()
    assert all(path.stat().st_size > 0 for path in recent)
