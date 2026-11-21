"""실제 macOS 프로세스의 실행 파일·명령·작업 폴더 식별을 검증한다."""

import os
import select
import subprocess
import sys
from pathlib import Path

import pytest

from src.bootstrap.instance import Role, identified_pid


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS ps와 lsof 확인이 필요하다")
@pytest.mark.parametrize(
    ("command", "role", "entrypoint", "foreign_cwd", "extra", "accepted"),
    [
        ("run", "resident", False, False, False, True),
        ("api", "resident", False, False, False, True),
        ("menubar", "menubar", False, False, False, True),
        ("run", "resident", True, False, False, True),
        ("menubar", "menubar", True, False, False, True),
        ("run", "menubar", False, False, False, False),
        ("menubar", "resident", False, False, False, False),
        ("run", "resident", False, True, False, False),
        ("run", "resident", False, False, True, False),
    ],
)
def test_identify_real_python_process(
    tmp_path: Path,
    command: str,
    role: Role,
    entrypoint: bool,
    foreign_cwd: bool,
    extra: bool,
    accepted: bool,
) -> None:
    """실제 인터프리터는 인정하되 역할·작업 폴더·추가 인수가 다르면 거부한다."""
    root = tmp_path / "owned"
    binary = root / ".venv/bin"
    binary.mkdir(parents=True)
    interpreter = binary / "python"
    interpreter.symlink_to(sys.executable)
    fixture = Path(__file__).resolve().parents[2] / "fixtures/identity_process.py"
    source = root / "src"
    source.mkdir()
    (source / "__init__.py").touch()
    (source / "__main__.py").write_bytes(fixture.read_bytes())
    script = binary / "maeng-mate"
    script.write_bytes(fixture.read_bytes())
    foreign = tmp_path / "foreign"
    foreign.mkdir()
    invocation = [str(script)] if entrypoint else ["-m", "src"]
    # 환경은 원천 자격 정보 없이 가상 역할 모듈을 찾는 경로만 전달한다.
    process = subprocess.Popen(
        [str(interpreter), *invocation, command, *(["extra"] if extra else [])],
        cwd=foreign if foreign_cwd else root,
        env={"PATH": os.defpath, "PYTHONPATH": str(root)},
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert process.stdout is not None
        assert select.select([process.stdout], [], [], 5)[0], "가상 역할 준비 시간 초과"
        assert process.stdout.readline().strip() == "ready"
        pidfile = tmp_path / "role.pid"
        pidfile.write_text(str(process.pid))
        assert identified_pid(pidfile, (root,), role) == (process.pid if accepted else None)
        # PID 재사용·엉뚱한 실행 환경은 같은 명령과 작업 폴더여도 소유하지 않는다.
        interpreter.unlink()
        interpreter.symlink_to("/usr/bin/false")
        assert identified_pid(pidfile, (root,), role) is None
    finally:
        process.terminate()
        process.communicate(timeout=5)
    assert identified_pid(pidfile, (root,), role) is None
