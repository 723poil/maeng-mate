"""메뉴바에서 비밀값을 조회하지 않는 인증 관리 클라이언트다."""

import httpx

from src.application.credentials import CredentialError, CredentialSecret
from src.application.notion_credentials import NotionCredentialState, parse_credential_state
from src.infrastructure.local_http import local_client, response_json


class NotionCredentialsApi:
    """프로바이더 토큰은 사용자가 입력한 저장 요청에서만 전송한다."""

    def __init__(self, address: str, api_token: str) -> None:
        """루프백 주소와 앱 API 인증만 주입한다."""
        self._address, self._api_token = address, api_token

    def read(self, *, allow_prompt: bool = False) -> NotionCredentialState:
        """등록·확인 상태만 조회한다."""
        if allow_prompt:
            return self._request("POST", None, action="/authorize")
        return self._request("GET", None)

    def check(self, revision: int) -> NotionCredentialState:
        """현재 선택된 토큰을 확인하도록 서버에 요청한다."""
        return self._request("POST", {"revision": revision}, action="/check")

    def save(self, revision: int, token: CredentialSecret) -> NotionCredentialState:
        """새 비밀값을 URL이나 명령행이 아닌 본문에 한 번 전송한다."""
        return self._request("PUT", {"revision": revision, "token": token.value})

    def delete(self, revision: int) -> NotionCredentialState:
        """현재 버전에 한해서 삭제한다."""
        return self._request("DELETE", {"revision": revision})

    def _request(
        self, method: str, body: dict[str, object] | None, *, action: str = ""
    ) -> NotionCredentialState:
        """실패 응답은 고정 분류만 읽고 자동 재전송하지 않는다."""
        try:
            with local_client(
                self._address, self._api_token, timeout=120 if action == "/authorize" else 12
            ) as client:
                path = "/v1/notion/credentials" + action
                response = client.request(method, path, json=body)
                payload: object = response_json(response, max_bytes=4096)
                if response.status_code != 200:
                    detail = payload.get("detail") if isinstance(payload, dict) else None
                    code = detail.get("code") if isinstance(detail, dict) else None
                    raise CredentialError(code if isinstance(code, str) else "unavailable")
                return parse_credential_state(payload)
        except httpx.TimeoutException:
            raise CredentialError("timeout") from None
        except (httpx.HTTPError, ValueError):
            raise CredentialError("unavailable") from None
