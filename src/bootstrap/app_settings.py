"""앱 지원 파일과 Keychain에서 시작에 필요한 연결 정보만 읽는다."""

import json
import os
import re
import secrets
import stat
import sys
from collections.abc import Mapping
from pathlib import Path

from src.application.credentials import CredentialError, CredentialSecret, CredentialStore
from src.bootstrap.environment import ConfigError, database_path
from src.bootstrap.instance import application_directory
from src.bootstrap.keychain import KeychainAccount
from src.infrastructure.private_files import write_private_file


def bootstrap_path() -> "Path":
    """실행 파일·Git 폴더 밖의 고정된 시작 정보 위치다."""
    return application_directory() / "bootstrap.json"


def data_environment(env: Mapping[str, str], *, profile_path: Path | None = None) -> dict[str, str]:
    """DB 위치·포트만 해석하며 비밀값과 DB는 읽지 않는다."""
    support = application_directory()
    defaults = {
        "DATABASE_PATH": str(support / "data/maeng-mate.sqlite3"),
        "LOCAL_API_PORT": "43127",
    }
    path = profile_path or bootstrap_path()
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except FileNotFoundError:
        pass
    except OSError:
        raise ConfigError("앱 시작 설정을 읽지 못했습니다.") from None
    else:
        try:
            with os.fdopen(descriptor, "rb") as stream:
                info = os.fstat(stream.fileno())
                if (
                    not stat.S_ISREG(info.st_mode)
                    or info.st_uid != os.getuid()
                    or info.st_mode & 0o077
                ):
                    raise ValueError
                data = stream.read(16385)
                if len(data) > 16384:
                    raise ValueError
                raw: object = json.loads(data)
            if (
                not isinstance(raw, dict)
                or type(raw.get("version")) is not int
                or raw["version"] not in {1, 2}
                or set(raw)
                != (
                    {"version", "databasePath", "privateConfigPath", "port"}
                    if raw["version"] == 1
                    else {"version", "databasePath", "port"}
                )
            ):
                raise ValueError
            db, port = raw["databasePath"], raw["port"]
            if not isinstance(db, str) or type(port) is not int or not 1 <= port <= 65535:
                raise ValueError
            defaults.update(
                DATABASE_PATH=database_path(db),
                LOCAL_API_PORT=str(port),
            )
        except (OSError, ValueError, TypeError):
            raise ConfigError("앱 시작 설정 형식·권한을 확인하세요.") from None
    values = dict(env)
    values.pop("PRIVATE_CONFIG_PATH", None)  # 이전 환경값도 실행 파일을 다시 읽게 하지 않는다.
    for key, value in defaults.items():
        if not values.get(key):
            values[key] = value
    return values


def save_bootstrap(env: Mapping[str, str], *, profile_path: Path | None = None) -> None:
    """비밀값 없이 위치·포트만 보호된 파일로 저장한다."""
    port = env.get("LOCAL_API_PORT", "43127")
    db = env.get("DATABASE_PATH")
    if not isinstance(db, str) or not re.fullmatch(r"[0-9]+", port) or not 1 <= int(port) <= 65535:
        raise ConfigError("앱 시작 설정을 확인하세요.")
    write_private_file(
        profile_path or bootstrap_path(),
        json.dumps(
            {
                "version": 2,
                "databasePath": database_path(db),
                "port": int(port),
            }
        ).encode(),
    )


def application_environment(
    env: Mapping[str, str],
    *,
    create_admin: bool = False,
    profile_path: Path | None = None,
    admin_store: CredentialStore | None = None,
) -> dict[str, str]:
    """소유 앱 실행에서만 Keychain 관리 키를 읽고 환경 대안을 덮어쓴다."""
    values = data_environment(env, profile_path=profile_path)
    if sys.platform != "darwin":
        return values
    try:
        store = admin_store if admin_store is not None else KeychainAccount.LOCAL_API.store()
        record = store.read()
        if record is not None:
            if record.token is None:
                raise CredentialError("keychain")
            values["LOCAL_API_TOKEN"] = record.token.value
        elif not values.get("LOCAL_API_TOKEN") and create_admin:
            saved = store.replace(0, CredentialSecret(secrets.token_urlsafe(32)))
            if saved.token is None:
                raise CredentialError("keychain")
            values["LOCAL_API_TOKEN"] = saved.token.value
    except CredentialError:
        raise ConfigError("앱 내부 인증 키의 Keychain 접근을 확인하세요.") from None
    return values
