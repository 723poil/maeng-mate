"""MCP 관리 상태와 발급 응답의 입력·출력 검증이다."""

import math

from src.application.mcp_access import McpAccessError, McpSettingsState
from src.domain.mcp_access import McpAccessSnapshot, McpAccessToken


def mcp_state_payload(state: McpSettingsState) -> dict[str, object]:
    """토큰 원문·해시 없이 상태를 직렬화한다."""
    snapshot = state.snapshot
    return {
        "revision": snapshot.revision,
        "enabled": snapshot.enabled,
        "ready": state.ready,
        "endpoint": state.endpoint,
        "observedAt": state.observed_at,
        "tokens": [
            {
                "id": item.id,
                "name": item.name,
                "createdAt": item.created_at,
                "expiresAt": item.expires_at,
                "revokedAt": item.revoked_at,
                "lastUsedAt": item.last_used_at,
            }
            for item in snapshot.tokens
        ],
    }


def _time(value: object, *, optional: bool = False) -> float | None:
    """JSON 시점에 불리언·무한대가 섞이지 않게 한다."""
    if optional and value is None:
        return None
    if (
        type(value) not in (int, float)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or not 0 <= value <= 253402214400
    ):
        raise McpAccessError("invalid")
    return float(value)


def parse_mcp_state(value: object) -> McpSettingsState:
    """클라이언트가 관리 응답을 임의 코드·비밀값으로 해석하지 않게 한다."""
    if not isinstance(value, dict) or set(value) != {
        "revision",
        "enabled",
        "ready",
        "endpoint",
        "observedAt",
        "tokens",
    }:
        raise McpAccessError("invalid")
    revision, enabled, ready, endpoint, items = (
        value[key] for key in ("revision", "enabled", "ready", "endpoint", "tokens")
    )
    observed = _time(value["observedAt"])
    if (
        type(revision) is not int
        or revision < 0
        or type(enabled) is not bool
        or type(ready) is not bool
        or not isinstance(endpoint, str)
        or not isinstance(items, list)
        or observed is None
    ):
        raise McpAccessError("invalid")
    tokens: list[McpAccessToken] = []
    for item in items:
        if not isinstance(item, dict) or set(item) != {
            "id",
            "name",
            "createdAt",
            "expiresAt",
            "revokedAt",
            "lastUsedAt",
        }:
            raise McpAccessError("invalid")
        identifier, name = item["id"], item["name"]
        created = _time(item["createdAt"])
        if not isinstance(identifier, str) or not isinstance(name, str) or created is None:
            raise McpAccessError("invalid")
        tokens.append(
            McpAccessToken(
                identifier,
                name,
                created,
                _time(item["expiresAt"], optional=True),
                _time(item["revokedAt"], optional=True),
                _time(item["lastUsedAt"], optional=True),
            )
        )
    return McpSettingsState(
        McpAccessSnapshot(revision, enabled, tuple(tokens)), ready, endpoint, observed
    )
