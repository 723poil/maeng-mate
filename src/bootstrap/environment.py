"""시작 경계의 환경변수만 검증한다."""

import re
from collections.abc import Mapping
from dataclasses import dataclass, field

from src.infrastructure.sqlite.paths import validate_database_path


class ConfigError(ValueError):
    """비밀값과 개인 경로를 포함하지 않는 설정 오류다."""


@dataclass(frozen=True)
class AppEnvironment:
    """시작 경계에서 검증한 앱 설정이다."""

    token: str = field(repr=False)
    port: int = 43127
    notion_token: str | None = field(default=None, repr=False)
    notion_user_id: str | None = field(default=None, repr=False)
    database: str | None = field(default=None, repr=False)
    relevance_policy: str = "participation"
    snapshot_max_age_seconds: int = 1200


@dataclass(frozen=True)
class McpEnvironment:
    """MCP는 조회 API의 연결 정보만 보유한다."""

    api_url: str
    api_token: str = field(repr=False)


def optional(env: Mapping[str, str], key: str) -> str | None:
    """빈 예제 값은 미설정으로, 공백뿐인 값은 오류로 처리한다."""
    value = env.get(key)
    if value is None or value == "":
        return None
    if not value.strip():
        raise ConfigError(f"설정을 확인하세요: {key}")
    return value


def required(env: Mapping[str, str], key: str) -> str:
    """필수 환경변수가 없으면 값 대신 변수명만 알린다."""
    value = optional(env, key)
    if value is None:
        raise ConfigError(f"설정을 확인하세요: {key}")
    return value


def api_token(value: str, name: str) -> str:
    """32자 이상의 공백 없는 ASCII 인증값만 허용한다."""
    if not re.fullmatch(r"[\x21-\x7e]{32,}", value):
        raise ConfigError(f"설정을 확인하세요: {name}")
    return value


def database_path(value: str) -> str:
    """공통 주소 검증의 오류를 환경변수 이름으로만 전달한다."""
    try:
        return validate_database_path(value)
    except ValueError:
        raise ConfigError("설정을 확인하세요: DATABASE_PATH") from None


def app_environment(env: Mapping[str, str]) -> AppEnvironment:
    """업무 코드가 직접 환경을 읽지 않도록 시작 시 한 번 변환한다."""
    port, token = local_api_settings(env)
    policy = optional(env, "WORK_RELEVANCE_POLICY") or "participation"
    if policy not in {"assignment", "participation"}:
        raise ConfigError("설정을 확인하세요: WORK_RELEVANCE_POLICY")
    db = optional(env, "DATABASE_PATH")
    age = optional(env, "SNAPSHOT_MAX_AGE_SECONDS") or "1200"
    if not re.fullmatch(r"[0-9]+", age) or not 1 <= int(age) <= 86400:
        raise ConfigError("설정을 확인하세요: SNAPSHOT_MAX_AGE_SECONDS")
    return AppEnvironment(
        token,
        port,
        optional(env, "NOTION_TOKEN"),
        optional(env, "NOTION_USER_ID"),
        database_path(db) if db else None,
        policy,
        snapshot_max_age_seconds=int(age),
    )


def local_api_settings(env: Mapping[str, str]) -> tuple[int, str]:
    """서버와 MCP가 같은 두 값만 검증해 사용한다."""
    token = api_token(required(env, "LOCAL_API_TOKEN"), "LOCAL_API_TOKEN")
    raw_port = optional(env, "LOCAL_API_PORT") or "43127"
    if not re.fullmatch(r"[0-9]+", raw_port) or not 1 <= int(raw_port) <= 65535:
        raise ConfigError("설정을 확인하세요: LOCAL_API_PORT")
    return int(raw_port), token


def mcp_environment(env: Mapping[str, str]) -> McpEnvironment:
    """조회 주소는 PC 루프백으로 고정하고 공통 포트·인증키만 읽는다."""
    supplied = optional(env, "MCP_ACCESS_TOKEN") or required(env, "LOCAL_API_TOKEN")
    port, token = local_api_settings({**env, "LOCAL_API_TOKEN": supplied})
    return McpEnvironment(f"http://127.0.0.1:{port}", token)


def sync_interval(env: Mapping[str, str]) -> int:
    """정기 수집을 명시적으로 실행할 때만 간격을 검사한다."""
    value = optional(env, "SYNC_INTERVAL_SECONDS") or "900"
    if not re.fullmatch(r"[0-9]+", value) or not 60 <= int(value) <= 86400:
        raise ConfigError("설정을 확인하세요: SYNC_INTERVAL_SECONDS (60~86400초)")
    return int(value)
