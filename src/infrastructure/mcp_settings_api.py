"""앱 내부 관리 키로만 MCP 설정을 조정하는 루프백 클라이언트다."""

import httpx

from src.application.credentials import validated_secret
from src.application.mcp_access import IssuedMcpToken, McpAccessError, McpSettingsState
from src.application.mcp_settings import parse_mcp_state
from src.infrastructure.local_http import local_client, response_json


class McpSettingsApi:
    """조회용 AI 토큰과 앱 관리 키를 혼용하지 않는다."""

    def __init__(self, address: str, token: str) -> None:
        """앱의 로컬 연결만 주입한다."""
        self._address, self._token = address, token

    def _request(self, method: str, path: str, body: dict[str, object] | None = None) -> object:
        """발급·폐기를 자동 반복하지 않고 응답 원문을 오류에 넣지 않는다."""
        try:
            with local_client(self._address, self._token, timeout=5) as client:
                response = client.request(method, path, json=body)
                if response.status_code != 200:
                    raise McpAccessError(
                        "conflict"
                        if response.status_code == 409
                        else "invalid"
                        if response.status_code == 422
                        else "missing"
                        if response.status_code == 404
                        else "storage"
                    )
                return response_json(response, max_bytes=1048576)
        except (httpx.HTTPError, ValueError):
            raise McpAccessError("storage") from None

    def read(self) -> McpSettingsState:
        """상태·주소·원문 없는 목록을 읽는다."""
        return parse_mcp_state(self._request("GET", "/v1/mcp/settings"))

    def set_enabled(self, revision: int, enabled: bool) -> McpSettingsState:
        """같은 설정 버전일 때만 사용 선택을 저장한다."""
        return parse_mcp_state(
            self._request("PUT", "/v1/mcp/settings", {"revision": revision, "enabled": enabled})
        )

    def issue(self, name: str, days: int | None) -> IssuedMcpToken:
        """발급 원문은 이 호출의 결과에만 포함한다."""
        result = self._request("POST", "/v1/mcp/tokens", {"name": name, "expiresInDays": days})
        if not isinstance(result, dict) or set(result) != {"state", "token", "id"}:
            raise McpAccessError("storage")
        state = parse_mcp_state(result["state"])
        identifier = result["id"]
        if not isinstance(identifier, str) or not any(
            item.id == identifier for item in state.snapshot.tokens
        ):
            raise McpAccessError("storage")
        return IssuedMcpToken(state, validated_secret(result["token"]), identifier)

    def revoke(self, identifier: str) -> McpSettingsState:
        """화면에서 선택한 토큰 ID만 폐기한다."""
        if len(identifier) != 32 or any(c not in "0123456789abcdef" for c in identifier):
            raise McpAccessError("invalid")
        return parse_mcp_state(self._request("POST", f"/v1/mcp/tokens/{identifier}/revoke"))
