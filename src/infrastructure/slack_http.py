"""Slack 인증 확인과 DM 전송의 공통 HTTP 경계를 관리한다."""

from typing import Literal

import httpx

from src.application.credentials import CredentialError, CredentialSecret
from src.infrastructure.api_decoding import json_object


def slack_post(
    method: Literal["auth.test", "chat.postMessage"],
    token: CredentialSecret,
    transport: httpx.BaseTransport | None,
    *,
    payload: dict[str, object] | None = None,
) -> dict[str, object]:
    """한 번만 요청하고 두 메서드에서 의미가 같은 명시적 거부만 공통 처리한다."""
    with httpx.Client(
        trust_env=False, follow_redirects=False, timeout=6, transport=transport
    ) as client:
        response = client.post(
            "https://slack.com/api/" + method,
            headers={"Authorization": "Bearer " + token.value},
            json=payload,
        )
        if response.status_code == 429:
            raise CredentialError("rate_limited")
        if response.status_code in {401, 403}:
            raise CredentialError("unauthorized")
        response.raise_for_status()
        if len(response.content) > 65536:
            raise ValueError
        raw: object = response.json()
    result = json_object(raw)
    if type(result.get("ok")) is not bool:
        raise ValueError
    if result["ok"] is False:
        error = result.get("error")
        if error in (
            "invalid_auth",
            "not_authed",
            "token_revoked",
            "token_expired",
            "account_inactive",
        ):
            raise CredentialError("unauthorized")
        if error in ("missing_scope", "not_allowed_token_type"):
            raise CredentialError("permission")
        if error == "ratelimited":
            raise CredentialError("rate_limited")
    return result
