"""잠금 파일의 Python 도구로 독립 알림 앱을 준비한다."""

import hashlib
import os
import shutil
import subprocess
from pathlib import Path

from src.bootstrap.environment import ConfigError
from src.infrastructure.private_files import write_private_file


def install_notification_bundle(source: Path, runtime: Path) -> None:
    """실행본과 분리된 번들을 빌드하고 성공한 결과만 앱 지원 폴더에 설치한다."""
    support = runtime.parent
    destination = support / "NotificationAgent.app"
    marker = support / "notification-build.json"
    inputs = (
        source / "packaging/notification.spec",
        source / "src/assets/app-icon.svg",
        source / "src/infrastructure/macos_icon.py",
        source / "src/infrastructure/notification_app/agent.py",
        source / "src/application/notion_links.py",
        source / "uv.lock",
    )
    digest = hashlib.sha256(b"".join(path.read_bytes() for path in inputs)).hexdigest()
    if destination.is_symlink():
        raise ConfigError("알림 앱 설치 경로를 확인하세요.")
    if destination.is_dir() and marker.is_file() and marker.read_text() == digest:
        return
    cache = source / ".cache/notification-app"
    cache.mkdir(parents=True, exist_ok=True, mode=0o700)
    environment = {
        key: os.environ[key] for key in ("PATH", "HOME", "LANG", "TMPDIR") if key in os.environ
    }
    environment["PYINSTALLER_CONFIG_DIR"] = str(source / ".cache/pyinstaller")
    try:
        result = subprocess.run(
            [
                str(source / ".venv/bin/python"),
                "-m",
                "PyInstaller",
                "--noconfirm",
                "--distpath",
                str(cache / "dist"),
                "--workpath",
                str(cache / "build"),
                str(source / "packaging/notification.spec"),
            ],
            cwd=source,
            env=environment,
            capture_output=True,
            timeout=180,
            check=False,
        )
        built = cache / "dist/NotificationAgent.app"
        if result.returncode != 0 or not (built / "Contents/MacOS/NotificationAgent").is_file():
            raise ConfigError(
                "알림 앱 빌드에 실패했습니다. 개발용 의존성과 빌드 환경을 확인하세요."
            )
        staged = support / ".NotificationAgent.staged.app"
        previous = support / ".NotificationAgent.previous.app"
        if staged.exists():
            shutil.rmtree(staged)
        shutil.copytree(built, staged, symlinks=True)
        if previous.exists():
            shutil.rmtree(previous)
        identity_marker = support / "notification-identity.json"
        saved_markers = {
            path: path.read_bytes() if path.is_file() else None
            for path in (marker, identity_marker)
        }
        if destination.exists():
            destination.rename(previous)
        try:
            staged.rename(destination)
            write_private_file(marker, digest.encode())
            identity_marker.unlink(missing_ok=True)
        except OSError:
            if destination.exists():
                shutil.rmtree(destination)
            if previous.exists():
                previous.rename(destination)
            for path, content in saved_markers.items():
                if content is None:
                    path.unlink(missing_ok=True)
                else:
                    write_private_file(path, content)
            raise
        if previous.exists():
            shutil.rmtree(previous)
    except (OSError, subprocess.TimeoutExpired):
        raise ConfigError("알림 앱을 설치하지 못했습니다.") from None
