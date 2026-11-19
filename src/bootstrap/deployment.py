"""보호된 Git 폴더와 로그인 실행본·데이터를 분리한다."""

import json
import os
import shutil
import sqlite3
import subprocess
from pathlib import Path

from src.bootstrap.app_settings import save_bootstrap
from src.bootstrap.environment import ConfigError, app_environment
from src.bootstrap.instance import application_directory, runtime_root
from src.bootstrap.notification_bundle import install_notification_bundle
from src.bootstrap.runtime_settings import load_runtime_environment
from src.infrastructure.private_files import write_private_file
from src.infrastructure.sqlite.backup import copy_database, discard_database_copy


def prepare_runtime(source: Path) -> Path:
    """현재 mise Python과 잠금 파일로 로그인 전용 실행 환경을 설치한다."""
    target = runtime_root()
    if application_directory().is_symlink() or target.is_symlink():
        raise ConfigError("자동 실행 설치 경로는 실제 디렉터리여야 합니다.")
    if source.resolve() == target.resolve():
        raise ConfigError("원본 Git 프로젝트에서 service install을 실행하세요.")
    uv = shutil.which("uv")
    interpreter = source / ".venv/bin/python"
    if uv is None or not interpreter.is_file():
        raise ConfigError("mise의 Python·uv 실행 환경을 확인하세요.")
    target.mkdir(parents=True, exist_ok=True, mode=0o700)
    code = target / "src"
    if code.is_symlink():
        raise ConfigError("자동 실행용 소스 경로를 확인하세요.")
    if code.exists():
        shutil.rmtree(code)
    shutil.copytree(source / "src", code, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    for name in ("pyproject.toml", "uv.lock"):
        write_private_file(target / name, (source / name).read_bytes())
    install_env = {
        key: os.environ[key]
        for key in ("PATH", "HOME", "LANG", "TMPDIR", "UV_CACHE_DIR", "UV_OFFLINE")
        if key in os.environ
    }
    try:
        result = subprocess.run(
            [
                uv,
                "sync",
                "--locked",
                "--extra",
                "menubar",
                "--no-dev",
                "--python",
                str(interpreter.resolve()),
                "--project",
                str(target),
            ],
            env=install_env,
            capture_output=True,
            text=True,
            timeout=180,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        raise ConfigError("자동 실행용 환경 설치를 완료하지 못했습니다.") from None
    if result.returncode != 0:
        raise ConfigError(
            "자동 실행용 의존성 설치에 실패했습니다. 네트워크와 uv 설치 상태를 확인하세요."
        )
    install_notification_bundle(source, target)
    return target


def move_active_data(source: Path, target: Path, values: dict[str, str]) -> None:
    """기존 DB를 보존하고 모든 준비가 끝난 뒤 활성 경로를 한 번에 전환한다."""
    environment = app_environment(load_runtime_environment(values))
    assert environment.database is not None
    current = Path(environment.database)
    support = target.parent
    data = support / "data"
    if data.is_symlink():
        raise ConfigError("자동 실행 데이터 경로는 실제 디렉터리여야 합니다.")
    data.mkdir(parents=True, exist_ok=True, mode=0o700)
    destination = data / "maeng-mate.sqlite3"
    manifest = support / "install.json"
    previous_manifest = manifest.read_bytes() if manifest.is_file() else None
    created = False
    manifest_written = False
    switched = False
    try:
        if current.resolve() != destination.resolve():
            try:
                copy_database(current, destination)
            except FileExistsError:
                raise ConfigError(
                    "자동 실행 데이터가 이미 있습니다. DATABASE_PATH를 확인하세요."
                ) from None
            except (OSError, sqlite3.Error):
                raise ConfigError("자동 실행용 DB 사본을 만들지 못했습니다.") from None
            created = True
        write_private_file(
            manifest,
            json.dumps(
                {"sourceRoot": str(source.resolve()), "runtimeRoot": str(target.resolve())}
            ).encode(),
        )
        manifest_written = True
        # 프로필 교체를 마지막에 해 실패한 설치가 새 DB를 활성화하지 않게 한다.
        save_bootstrap(
            {**values, "DATABASE_PATH": str(destination)},
            profile_path=support / "bootstrap.json",
        )
        switched = True
    finally:
        if not switched:
            try:
                if manifest_written:
                    if previous_manifest is None:
                        manifest.unlink()
                    else:
                        write_private_file(manifest, previous_manifest)
            finally:
                if created:
                    discard_database_copy(destination)
