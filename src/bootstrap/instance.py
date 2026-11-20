"""같은 프로젝트의 서버·메뉴바 중복 실행을 차단한다."""

import fcntl
import json
import os
import signal
import subprocess
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Literal

from src.bootstrap.environment import ConfigError
from src.bootstrap.executable import bundle_contents, packaged
from src.infrastructure.private_files import write_private_file

type Role = Literal["resident", "menubar"]


def project_root() -> Path:
    """현재 소스가 속한 프로젝트를 기준으로 실행 파일을 찾는다."""
    return bundle_contents() if packaged() else Path(__file__).resolve().parents[2]


def application_directory() -> Path:
    """로그인 서비스가 접근 가능한 사용자 전용 저장 위치다."""
    return Path.home() / "Library/Application Support/MaengMate"


def runtime_root() -> Path:
    """Git 폴더와 분리한 자동 실행용 앱 경로다."""
    return application_directory() / "app"


def state_directory() -> Path:
    """수동 실행과 배포 실행이 같은 잠금·PID를 사용한다."""
    return (
        application_directory() / "run" if sys.platform == "darwin" else project_root() / ".cache"
    )


def request_panel() -> None:
    """메뉴바가 다음 화면 갱신 때 소비할 열기 요청을 게시한다."""
    (state_directory() / "open-panel").touch(mode=0o600)


def owned_roots(root: Path) -> tuple[Path, ...]:
    """현재 프로젝트와 설치 때 기록한 원본·실행본만 관리 대상으로 삼는다."""
    roots = {root.resolve(), runtime_root().resolve(), application_directory().resolve()}
    try:
        manifest = application_directory() / "install.json"
        info = manifest.stat()
        if info.st_uid == os.getuid() and not info.st_mode & 0o077:
            value: object = json.loads(manifest.read_text())
            if isinstance(value, dict):
                for key in ("sourceRoot", "bundleRoot"):
                    source = value.get(key)
                    if isinstance(source, str):
                        roots.add(Path(source).resolve())
    except (OSError, ValueError):
        pass
    return tuple(roots)


@contextmanager
def _state_lock(
    path: Path, *, shared: bool, busy_message: str, wait: bool = False
) -> Iterator[None]:
    """요청한 대기 방식으로 잠금을 획득하며 본문이나 정리가 실패해도 파일을 닫는다."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        operation = fcntl.LOCK_SH if shared else fcntl.LOCK_EX
        try:
            fcntl.flock(descriptor, operation if wait else operation | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ConfigError(busy_message) from None
        try:
            yield
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
    finally:
        os.close(descriptor)


@contextmanager
def instance_lock(role: Role) -> Iterator[None]:
    """역할 잠금을 보유한 동안에만 이 프로세스의 PID를 게시한다."""
    directory = state_directory()
    pidfile = directory / (role + ".pid")
    pid = str(os.getpid())
    with _state_lock(
        directory / (role + ".lock"), shared=False, busy_message="이미 실행 중입니다: " + role
    ):
        write_private_file(pidfile, pid.encode())
        try:
            yield
        finally:
            # 잠금을 놓기 전에 자신이 게시한 PID만 지운다.
            if pidfile.exists() and pidfile.read_text().strip() == pid:
                pidfile.unlink()


def owned_pid(root: Path, role: Role) -> int | None:
    """PID만 믿지 않고 실행 파일·명령·작업 폴더까지 확인한다."""
    roots = owned_roots(root)
    candidates = (
        state_directory() / (role + ".pid"),
        *(path / ".cache" / (role + ".pid") for path in roots),
    )
    for candidate in candidates:
        pid = identified_pid(candidate, roots, role)
        if pid is not None:
            return pid
    return None


def identified_pid(pidfile: Path, roots: tuple[Path, ...], role: Role) -> int | None:
    """오래된 PID나 다른 프로젝트를 종료 대상으로 삼지 않는다."""
    try:
        pid = int(pidfile.read_text())
        if pid <= 0:
            return None
        result = subprocess.run(
            ["/bin/ps", "-ww", "-p", str(pid), "-o", "command="],
            capture_output=True,
            text=True,
            timeout=3,
            check=False,
        )
        if result.returncode != 0:
            return None
        commands = ("run", "api") if role == "resident" else ("menubar",)
        interpreters = tuple(root / ".venv/bin/python" for root in roots)
        executables = {
            path
            for interpreter in interpreters
            for path in (
                str(interpreter),
                str(interpreter.resolve()),
                str(interpreter.with_name("python3")),
            )
        }
        expected = {
            invocation
            for executable in executables
            for command in commands
            for root in roots
            for invocation in (
                f"{executable} -m src {command}",
                f"{executable} {root / '.venv/bin/maeng-mate'} {command}",
            )
        }
        expected.update(
            f"{candidate / 'MacOS/MaengMate'} {command}"
            for candidate in roots
            for command in commands
            if (candidate / "MacOS/MaengMate").is_file()
        )
        if result.stdout.strip() not in expected:
            return None
        location = subprocess.run(
            ["/usr/sbin/lsof", "-a", "-p", str(pid), "-d", "cwd", "-Fn"],
            capture_output=True,
            text=True,
            timeout=3,
            check=False,
        )
        paths = [
            Path(line[1:]).resolve()
            for line in location.stdout.splitlines()
            if line.startswith("n")
        ]
        return pid if any(root in paths for root in roots) else None
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return None


def stop_owned_process(root: Path, role: Role, *, expected_pid: int | None = None) -> None:
    """등록 전의 수동 실행도 현재 프로젝트 소유가 확인된 경우에만 종료한다."""
    pid = owned_pid(root, role)
    if pid is None or (expected_pid is not None and pid != expected_pid):
        return
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    deadline = time.monotonic() + 18
    while time.monotonic() < deadline:
        if owned_pid(root, role) != pid:
            return
        time.sleep(0.1)
    raise ConfigError("앱 종료를 확인하지 못했습니다: " + role)


@contextmanager
def coordination_lock(
    name: Literal["storage", "service", "restore-trial"],
    *,
    shared: bool = False,
    wait: bool = False,
) -> Iterator[None]:
    """데이터 전환과 서비스 관리가 다른 실행과 겹치지 않게 한다."""
    with _state_lock(
        state_directory() / (name + ".lock"),
        shared=shared,
        wait=wait,
        busy_message="다른 저장 또는 서비스 작업이 진행 중입니다. 완료 후 다시 실행하세요.",
    ):
        yield
