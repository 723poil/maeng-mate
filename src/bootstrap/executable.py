"""소스 실행과 동봉 macOS 앱의 자기 실행 명령을 구분한다."""

import sys
from collections.abc import Mapping
from pathlib import Path

OS_ENV_KEYS = (
    "PATH",
    "HOME",
    "LANG",
    "LC_ALL",
    "TMPDIR",
    "__CF_USER_TEXT_ENCODING",
    "PYTHONUTF8",
    "PYTHONIOENCODING",
)


def packaged() -> bool:
    """개발 환경을 추측하지 않고 PyInstaller의 실행 표시만 확인한다."""
    return bool(getattr(sys, "frozen", False))


def executable_command(*arguments: str, source_root: Path | None = None) -> tuple[str, ...]:
    """개발 서비스는 지정한 가상환경, 배포 앱은 현재 번들의 실행 파일을 사용한다."""
    if packaged():
        return (sys.executable, *arguments)
    interpreter = (
        str(source_root / ".venv/bin/python") if source_root is not None else sys.executable
    )
    return (interpreter, "-m", "src", *arguments)


def execution_environment(inherited: Mapping[str, str]) -> dict[str, str]:
    """자식에게 필요한 OS 환경만 상속하고 동봉 앱은 독립 실행으로 시작한다."""
    result = {key: inherited[key] for key in OS_ENV_KEYS if key in inherited}
    if packaged():
        result["PYINSTALLER_RESET_ENVIRONMENT"] = "1"
    return result


def bundle_contents() -> Path:
    """앱 실행 파일이 위치한 Contents를 실제 경로로 반환한다."""
    return Path(sys.executable).resolve().parent.parent
