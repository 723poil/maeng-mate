"""동봉 macOS 앱의 최초 준비와 로그인 실행을 연결한다."""

import asyncio
import json
import os
import shutil
import sqlite3
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path

from src.bootstrap.app_settings import application_environment, save_bootstrap
from src.bootstrap.environment import ConfigError, required
from src.bootstrap.executable import bundle_contents
from src.bootstrap.instance import application_directory
from src.bootstrap.work_settings import initialize_work_settings
from src.domain.runtime_settings import RuntimeSettings
from src.infrastructure.private_files import write_private_file
from src.infrastructure.sqlite.backup import copy_database
from src.infrastructure.sqlite.connection import Database
from src.infrastructure.sqlite.runtime_settings import SQLiteRuntimeSettings
from src.infrastructure.sqlite.schema import migrate


def initialize_database(path: str) -> None:
    """새 설정만 만들고 기존 근태·소스·일반 설정을 덮지 않는다."""
    database = Database(path)
    asyncio.run(migrate(database))
    initialize_work_settings(database)
    SQLiteRuntimeSettings(database).initialize(RuntimeSettings())


def prepare_connection() -> dict[str, str]:
    """중지 전에 연결 값과 명시적 Keychain 접근만 확인하며 DB를 변경하지 않는다."""
    if application_directory().is_symlink():
        raise ConfigError("앱 지원 폴더의 실제 경로를 확인하세요.")
    # 설치에 꼭 필요한 내부 키만 승인하고 연동 키는 각 설정 화면에서 요청한다.
    values = application_environment({}, create_admin=True, allow_prompt=True)
    save_bootstrap(values)
    return values


def backup_database(path: str) -> None:
    """중지·잠금을 마친 기존 DB를 WAL까지 포함해 개인 백업으로 보존한다."""
    source = Path(path)
    if not source.exists():
        return
    directory = application_directory() / "backups"
    if directory.is_symlink():
        raise ConfigError("백업 폴더의 실제 경로를 확인하세요.")
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    target = directory / ("pre-install-" + stamp + ".sqlite3")
    try:
        copy_database(source, target)
    except (OSError, sqlite3.Error):
        raise ConfigError("기존 DB를 백업하지 못했습니다. 설치를 중단합니다.") from None


def prepare_state(values: Mapping[str, str]) -> None:
    """서비스 중지와 저장 잠금 안에서만 기존 DB를 백업하고 갱신한다."""
    path = required(values, "DATABASE_PATH")
    backup_database(path)
    initialize_database(path)


def install_resources() -> None:
    """동봉 알림 원본을 쓰기 가능한 지원 폴더로 복사하고 설치 위치를 기록한다."""
    contents = bundle_contents()
    support = application_directory()
    source = contents / "Resources/NotificationAgent.app"
    if not (source / "Contents/MacOS/NotificationAgent").is_file():
        raise ConfigError("동봉 알림 앱을 찾지 못했습니다. 앱을 다시 설치하세요.")
    destination = support / "NotificationAgent.app"
    staged = support / ".NotificationAgent.packaged.app"
    previous = support / ".NotificationAgent.previous.app"
    if any(path.is_symlink() for path in (support, destination, staged, previous)):
        raise ConfigError("앱 지원 폴더의 실제 경로를 확인하세요.")
    if staged.exists():
        shutil.rmtree(staged)
    shutil.copytree(source, staged, symlinks=True)
    if previous.exists():
        shutil.rmtree(previous)
    if destination.exists():
        destination.rename(previous)
    try:
        staged.rename(destination)
    except OSError:
        if previous.exists():
            previous.rename(destination)
        raise ConfigError("알림 앱 설치를 완료하지 못했습니다.") from None
    if previous.exists():
        shutil.rmtree(previous)
    (support / "notification-identity.json").unlink(missing_ok=True)


def record_installation() -> None:
    """정의 파일 등록·실제 API 준비가 끝난 설치만 완료로 표시한다."""
    support = application_directory()
    contents = bundle_contents()
    manifest = support / "install.json"
    value: dict[str, object] = {}
    if manifest.is_file() and not manifest.is_symlink():
        info = manifest.stat()
        if info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise ConfigError("앱 설치 기록의 권한을 확인하세요.")
        raw: object = json.loads(manifest.read_bytes())
        if not isinstance(raw, dict):
            raise ConfigError("앱 설치 기록을 확인하세요.")
        value = {key: item for key, item in raw.items() if isinstance(key, str)}
    value.update(
        bundleRoot=str(contents), bundleBuild=(contents / "Resources/build-info.json").read_text()
    )
    write_private_file(manifest, json.dumps(value).encode())


def launch_action() -> str:
    """앱 첫 실행은 필요한 서비스만 설치하고 동일 빌드의 재실행은 그대로 사용한다."""

    contents = bundle_contents()
    if "Volumes" in contents.parts or "AppTranslocation" in contents.parts:
        raise ConfigError("Maeng Mate를 응용 프로그램 폴더로 옮긴 뒤 실행하세요.")
    manifest = application_directory() / "install.json"
    action = "install"
    try:
        if manifest.is_file() and not manifest.is_symlink():
            info = manifest.stat()
            if info.st_uid == os.getuid() and not info.st_mode & 0o077:
                raw: object = json.loads(manifest.read_bytes())
                if (
                    isinstance(raw, dict)
                    and raw.get("bundleRoot") == str(contents)
                    and raw.get("bundleBuild")
                    == (contents / "Resources/build-info.json").read_text()
                ):
                    action = "start"
    except (OSError, ValueError):
        pass
    return action


def launch() -> None:
    """Finder 실행 요청을 직렬화하고 준비된 메뉴바에 패널 열기를 전달한다."""
    from src.bootstrap.service import run_service

    run_service("finder")
