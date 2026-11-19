"""Slack auth.test로 봇 인증만 확인한다."""

import httpx

from src.application.credentials import CredentialError, CredentialSecret
from src.application.slack_settings import SlackIdentity
from src.infrastructure.slack_http import slack_post


class SlackAuth:
    """토큰을 URL·본문·로그에 쓰지 않고 고정된 Slack 인증 API만 호출한다."""

    def __init__(self, *, transport: httpx.BaseTransport | None = None) -> None:
        """실제 환경은 기본 HTTPS 전송, 검증은 외부 fixture 전송을 주입한다."""
        self._transport = transport

    def verify(self, token: CredentialSecret) -> SlackIdentity:
        """원문 없는 오류 코드와 봇 표시 정보만 반환한다."""
        try:
            raw = slack_post("auth.test", token, self._transport)
            if not raw["ok"]:
                raise CredentialError("unavailable")
            bot_id = raw.get("bot_id")
            if not isinstance(bot_id, str) or not bot_id:
                raise CredentialError("permission")
            workspace, bot = raw.get("team"), raw.get("user")
            if any(isinstance(value, str) and len(value) > 200 for value in (workspace, bot)):
                raise CredentialError("unavailable")
            return SlackIdentity(
                workspace if isinstance(workspace, str) else None,
                bot if isinstance(bot, str) else None,
            )
        except httpx.TimeoutException:
            raise CredentialError("timeout") from None
        except (httpx.HTTPError, ValueError):
            raise CredentialError("unavailable") from None
