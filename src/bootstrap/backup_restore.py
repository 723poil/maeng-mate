"""전체 복원 시 서비스 정지·저장 잠금·준비 확인과 이전 상태 복구를 조율한다."""

import json
import os
import subprocess
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory

from src.application.data_backup import BackupPreview
from src.bootstrap.app_settings import application_environment, data_environment
from src.bootstrap.backup_commands import stage_archive
from src.bootstrap.backup_transaction import (
    begin_restore,
    finish_restore,
    pending_restore,
    rollback_restore,
)
from src.bootstrap.environment import ConfigError
from src.bootstrap.executable import executable_command, execution_environment
from src.bootstrap.instance import (
    application_directory,
    coordination_lock,
    instance_lock,
    project_root,
    stop_owned_process,
)
from src.bootstrap.service import LABELS, preflight, start_apps, wait_ready
from src.infrastructure.macos_launchd import MacLaunchd
from src.infrastructure.sqlite.restore_notifications import prepare_restored_notifications


def _manager() -> MacLaunchd:
    """현재 사용자의 로그인 서비스만 대상으로 한다."""
    return MacLaunchd(os.getuid(), Path.home() / "Library/LaunchAgents")


@contextmanager
def _offline() -> Iterator[None]:
    """두 앱과 수동 저장 작업이 모두 멈춘 동안만 실제 파일을 변경한다."""
    with instance_lock("resident"), instance_lock("menubar"), coordination_lock("storage"):
        yield


def _stop_for_restore(manager: MacLaunchd, root: Path) -> None:
    """다음 로그인 자동 실행은 유지한 채 이번 세션의 앱만 중지한다."""
    for role in reversed(LABELS):
        manager.unload(LABELS[role])
        stop_owned_process(root, role)


def recover_before_start(manager: MacLaunchd, root: Path) -> None:
    """서비스 관리 잠금 안에서 중단된 교체를 정리한 뒤 일반 시작을 허용한다."""
    home = application_directory()
    if pending_restore(home):
        _stop_for_restore(manager, root)
        with _offline():
            rollback_restore(home, Path(data_environment({})["DATABASE_PATH"]))


def guard_startup(*, allow_trial: bool = False) -> None:
    """미완료 복원에서는 일반 접근을 막고 로그인 시 독립 도우미가 복구하게 한다."""
    if not pending_restore(application_directory()):
        return
    if allow_trial:
        try:
            with coordination_lock("restore-trial", shared=True):
                # 완료 확인과 잠금 획득 사이에 복원이 확정되었다면 정상 실행을 이어간다.
                if not pending_restore(application_directory()):
                    return
        except ConfigError:
            # 교체를 끝낸 도우미가 새 서비스의 준비 상태를 확인하는 구간이다.
            return
        subprocess.Popen(
            executable_command("restore", "recover"),
            cwd=project_root(),
            env=execution_environment(os.environ),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    raise ConfigError("중단된 데이터 복원을 정리한 뒤 앱을 시작하세요.")


def restore_selected(path: Path, preview: BackupPreview) -> None:
    """확인한 백업으로 교체하고 두 역할과 인증 API가 준비되어야 확정한다."""
    home, root, manager = application_directory(), project_root(), _manager()
    with TemporaryDirectory() as temporary:
        directory = Path(temporary)
        if stage_archive(path, directory) != preview:
            raise ConfigError("선택한 백업 파일이 바뀌었습니다. 다시 선택하세요.")
        staged = directory / "contents"
        prepare_restored_notifications(staged / "data.sqlite3")
        with coordination_lock("service"):
            recover_before_start(manager, root)
            if not all(manager.path(label).is_file() for label in LABELS.values()):
                raise ConfigError("자동 실행 등록을 확인한 뒤 다시 불러오세요.")
            values = preflight()
            database = Path(values["DATABASE_PATH"])
            ready_to_commit = False
            try:
                _stop_for_restore(manager, root)
                with _offline():
                    begin_restore(home, database, staged)
                # 시험 시작을 허용하는 잠금은 프로세스가 죽으면 OS가 해제한다.
                with coordination_lock("restore-trial"):
                    expected = start_apps(manager, root)
                    wait_ready(manager, root, values, expected)
                    ready_to_commit = True
                    finish_restore(home)
            except Exception:
                if ready_to_commit and not pending_restore(home):
                    # 확정 표식을 옮긴 이후의 디스크 오류를 롤백 성공으로 보고하지 않는다.
                    raise ConfigError(
                        "데이터를 적용했지만 복원 완료 상태를 확인하지 못했습니다."
                    ) from None
                try:
                    recover_before_start(manager, root)
                    previous = start_apps(manager, root)
                    wait_ready(manager, root, values, previous)
                except Exception:
                    raise ConfigError(
                        "복원을 마치지 못했습니다. 앱을 다시 열어 복구하세요."
                    ) from None
                raise ConfigError(
                    "복원하지 못해 복원 직전 데이터로 앱을 다시 시작했습니다."
                ) from None


def _read_request() -> tuple[Path, BackupPreview]:
    """파이프로 넘긴 파일 경로와 사용자가 확인한 미리보기만 허용한다."""
    data = sys.stdin.buffer.read(32769)
    if len(data) > 32768:
        raise ValueError
    raw: object = json.loads(data)
    if not isinstance(raw, dict) or set(raw) != {"file", "stamp", "digest"}:
        raise ValueError
    path, stamp, digest = raw["file"], raw["stamp"], raw["digest"]
    if not isinstance(path, str) or not isinstance(stamp, str) or not isinstance(digest, str):
        raise ValueError
    if not Path(path).is_absolute():
        raise ValueError
    return Path(path), BackupPreview(stamp, digest)


def run_restore(action: str | None) -> None:
    """로그인 복구와 사용자가 명시적으로 요청한 복원을 독립 프로세스에서 수행한다."""
    if action == "recover":
        with coordination_lock("service"):
            if pending_restore(application_directory()):
                manager, root = _manager(), project_root()
                recover_before_start(manager, root)
                expected = start_apps(manager, root)
                wait_ready(manager, root, application_environment({}), expected)
        return
    if action != "apply":
        raise ConfigError("설정의 백업 불러오기에서 파일을 선택하세요.")
    from src.presentation.backup_result import show_restore_result

    try:
        restore_selected(*_read_request())
    except Exception as error:
        message = (
            str(error)
            if isinstance(error, ConfigError)
            else "백업 파일과 사용 가능한 디스크 공간을 확인하세요."
        )
        show_restore_result("데이터 복원 실패", message)
        raise ConfigError("데이터 복원을 완료하지 못했습니다.") from None
    show_restore_result(
        "데이터를 복원했습니다.", "백업 시점의 기록과 설정으로 앱을 다시 시작했습니다."
    )
