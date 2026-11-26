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
        assert not list((tmp_path / ".pre-install-retiring").iterdir())
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


def test_interrupted_retirement_preserves_other_links(tmp_path: Path) -> None:
    """정리 실패 뒤 공개 이름을 다시 쓰거나 보관 링크를 만들어도 다음 성공 때 보존한다."""
    database = tmp_path / "data.sqlite3"
    packaged_app.initialize_database(str(database))
    backups = tmp_path / "backups"
    queue = tmp_path / ".pre-install-retiring"
    with patch.object(packaged_app, "application_directory", return_value=tmp_path):
        for _ in range(8):
            packaged_app.backup_database(str(database))
    with patch.object(backup_retention, "_finish_claim", side_effect=OSError("interrupted")):
        for _ in range(3):
            backup_retention.prune_copies(tmp_path, "pre-install")
            assert len(list(queue.iterdir())) == 1

    (claimed,) = queue.iterdir()
    original = backups / claimed.name.split(" ")[0]
    original.write_bytes(b"manual replacement")
    saved = backups / "my-recovery.sqlite3"
    saved.hardlink_to(claimed)
    expected = saved.read_bytes()
    assert expected.startswith(b"SQLite format 3")
    backup_retention.prune_copies(tmp_path, "pre-install")
    assert original.read_bytes() == b"manual replacement"
    assert saved.read_bytes() == expected
    assert not list(queue.iterdir())
    assert len(list(backups.glob("pre-install-*.sqlite3"))) == 4
    assert not list(backups.glob(".retired-*"))


@pytest.mark.parametrize("occupied", [False, True])
def test_retry_preserves_changed_claim(tmp_path: Path, occupied: bool) -> None:
    """중단 중 바뀐 항목은 원래 이름으로 복구하고 양쪽 이름이 쓰이면 그대로 보존한다."""
    database = tmp_path / "data.sqlite3"
    packaged_app.initialize_database(str(database))
    backups = tmp_path / "backups"
    queue = tmp_path / ".pre-install-retiring"
    with patch.object(packaged_app, "application_directory", return_value=tmp_path):
        for _ in range(5):
            packaged_app.backup_database(str(database))
    with patch.object(backup_retention, "_finish_claim", side_effect=OSError("interrupted")):
        backup_retention.prune_copies(tmp_path, "pre-install")
    (claimed,) = queue.iterdir()
    original = backups / claimed.name.split(" ")[0]
    replacement = tmp_path / "manual"
    replacement.write_bytes(b"changed during interruption")
    replacement.replace(claimed)
    if occupied:
        original.write_bytes(b"another manual file")
        backup_retention.prune_copies(tmp_path, "pre-install")
        assert original.read_bytes() == b"another manual file"
        assert claimed.read_bytes() == b"changed during interruption"
        original.rename(tmp_path / "saved")
    backup_retention.prune_copies(tmp_path, "pre-install")
    assert original.read_bytes() == b"changed during interruption"
    assert not list(queue.iterdir())


def test_unknown_queue_entry(tmp_path: Path) -> None:
    """소유 기록과 맞지 않는 기존 항목을 보존하고 새 대기 항목을 추가하지 않는다."""
    database = tmp_path / "data.sqlite3"
    packaged_app.initialize_database(str(database))
    backups = tmp_path / "backups"
    queue = tmp_path / ".pre-install-retiring"
    queue.mkdir(mode=0o700)
    unknown = queue / "unknown"
    unknown.write_bytes(b"keep")
    with patch.object(packaged_app, "application_directory", return_value=tmp_path):
        for _ in range(5):
            packaged_app.backup_database(str(database))
    backup_retention.prune_copies(tmp_path, "pre-install")
    assert unknown.read_bytes() == b"keep"
    assert list(queue.iterdir()) == [unknown]
    assert len(list(backups.glob("pre-install-*.sqlite3"))) == 5
    unknown.rename(tmp_path / "saved")
    backup_retention.prune_copies(tmp_path, "pre-install")
    assert not list(queue.iterdir())
    assert len(list(backups.glob("pre-install-*.sqlite3"))) == 3


def test_retry_after_removing_claim(tmp_path: Path) -> None:
    """대기 항목 제거 후 생성 기록 갱신이 중단돼도 남은 사본부터 정리를 이어간다."""
    database = tmp_path / "data.sqlite3"
    packaged_app.initialize_database(str(database))
    backups = tmp_path / "backups"
    queue = tmp_path / ".pre-install-retiring"
    with patch.object(packaged_app, "application_directory", return_value=tmp_path):
        for _ in range(6):
            packaged_app.backup_database(str(database))
    recent = sorted(backups.glob("*.sqlite3"))[-3:]
    finish = backup_retention._finish_claim

    def interrupt(directory: int, pending: int, entry: str, lines: list[str]) -> None:
        """항목 제거가 끝난 시점에 오류를 발생시켜 이전 생성 기록을 유지한다."""
        finish(directory, pending, entry, lines)
        raise OSError("interrupted")

    with patch.object(backup_retention, "_finish_claim", side_effect=interrupt):
        backup_retention.prune_copies(tmp_path, "pre-install")
    assert not list(queue.iterdir())
    assert len(list(backups.glob("*.sqlite3"))) == 5
    backup_retention.prune_copies(tmp_path, "pre-install")
    assert sorted(backups.glob("*.sqlite3")) == recent
    assert not list(queue.iterdir())
