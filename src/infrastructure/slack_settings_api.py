"""설정 화면이 호출하는 Slack 관리 API 클라이언트다."""

import httpx

from src.application.credentials import CredentialError, CredentialSecret
from src.application.errors import StatusReadError
from src.application.slack_settings import SlackSettingsState, SlackTestError, parse_slack_state
from src.infrastructure.local_http import local_client, response_json


class SlackSettingsApi:
    """DB·Keychain 접근 없이 공통 로컬 관리 키만 사용한다."""

    def __init__(self, address: str, token: str) -> None:
        """루프백 주소와 앱 내부 인증 키를 주입한다."""
        self._address, self._token = address, token

    def _request(
        self, action: str | None = None, payload: dict[str, object] | None = None
    ) -> SlackSettingsState:
        """모호한 쓰기를 반복하지 않고 비밀값 없는 오류만 전달한다."""
        try:
            with local_client(
                self._address, self._token, timeout=120 if action == "authorize" else 15
            ) as client:
                response = (
                    client.get("/v1/slack/settings")
                    if action is None
                    else client.post("/v1/slack/settings/" + action, json=payload)
                )
                raw: object = response_json(response, max_bytes=16384)
                if response.status_code >= 400:
                    if isinstance(raw, dict) and isinstance(raw.get("detail"), dict):
                        code = raw["detail"].get("code")
                        if code == "delivery_unknown":
                            raise SlackTestError("delivery_unknown")
                        if code == "recipient":
                            raise SlackTestError("recipient")
                        if code == "unverified":
                            raise SlackTestError("unverified")
                        if isinstance(code, str):
                            raise CredentialError(code)
                    raise ValueError
                return parse_slack_state(raw)
        except (CredentialError, SlackTestError):
            raise
        except (httpx.HTTPError, ValueError):
            raise StatusReadError("Slack 설정 연결을 확인하세요.") from None

    def read(self, *, allow_prompt: bool = False) -> SlackSettingsState:
        """등록·수신자·확인 상태만 읽는다."""
        if allow_prompt:
            return self._request("authorize", {})
        return self._request()

    def save_member(self, revision: int, user_id: str | None) -> SlackSettingsState:
        """수신자 하나를 같은 일반 설정 버전으로 저장한다."""
        return self._request("member", {"revision": revision, "userId": user_id})

    def save_token(self, revision: int, token: CredentialSecret) -> SlackSettingsState:
        """토큰은 요청 본문에만 담고 상태 응답으로 되받지 않는다."""
        return self._request("token", {"revision": revision, "token": token.value})

    def check(self, revision: int) -> SlackSettingsState:
        """새 입력이 아닌 저장된 토큰을 확인한다."""
        return self._request("check", {"revision": revision})

    def delete_token(self, revision: int) -> SlackSettingsState:
        """토큰만 삭제하고 본인 멤버 ID는 유지한다."""
        return self._request("delete", {"revision": revision})

    def save_app_token(self, revision: int, token: CredentialSecret) -> SlackSettingsState:
        """앱 토큰을 전용 보안 저장 경로에 전달한다."""
        return self._request("app-token", {"revision": revision, "token": token.value})

    def delete_app_token(self, revision: int) -> SlackSettingsState:
        """앱 토큰의 현재 버전에만 삭제를 요청한다."""
        return self._request("app-delete", {"revision": revision})

    def test_notification(self, revision: int, credential_revision: int) -> SlackSettingsState:
        """저장된 버전만 전달하며 재시도 없이 테스트 발송을 요청한다."""
        return self._request(
            "test", {"revision": revision, "credentialRevision": credential_revision}
        )
