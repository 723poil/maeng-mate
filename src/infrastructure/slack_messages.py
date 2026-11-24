"""테스트와 자동 알림이 공유하는 Slack DM 전송이다."""

from html import escape

import httpx

from src.application.credentials import CredentialError, CredentialSecret
from src.application.notion_links import notion_link
from src.application.slack_settings import SlackTestError
from src.infrastructure.slack_http import slack_post


class SlackMessages:
    """고정된 API·본문만 사용하고 전송을 자동 재시도하지 않는다."""

    def __init__(self, *, transport: httpx.BaseTransport | None = None) -> None:
        """검증용 전송만 주입하며 운영에서는 기본 HTTPS를 사용한다."""
        self._transport = transport

    def send_test(self, token: CredentialSecret, user_id: str) -> None:
        """테스트는 고정 문구만 사용한다."""
        self.send(token, user_id, "Catch — 테스트 알림입니다.")

    def send(
        self, token: CredentialSecret, user_id: str, text: str, *, url: str | None = None
    ) -> None:
        """접수 성공과 응답 유실을 구분하며 제목·본문을 일반 텍스트로 보낸다."""
        try:
            raw = slack_post(
                "chat.postMessage",
                token,
                self._transport,
                payload=message_payload(user_id, text, url),
            )
            if raw["ok"]:
                if not isinstance(raw.get("ts"), str) or not isinstance(raw.get("channel"), str):
                    raise SlackTestError("delivery_unknown")
                return
            error = raw.get("error")
            if error in ("no_permission", "restricted_action"):
                raise CredentialError("permission")
            if error in ("channel_not_found", "user_not_found", "messages_tab_disabled"):
                raise SlackTestError("recipient")
            if error == "rate_limited":
                raise CredentialError("rate_limited")
            raise SlackTestError("delivery_unknown")
        except (httpx.HTTPError, ValueError):
            # 응답을 못 받았어도 전송됐을 수 있으므로 실패 단정·재전송을 하지 않는다.
            raise SlackTestError("delivery_unknown") from None


def message_payload(user_id: str, text: str, url: str | None) -> dict[str, object]:
    """검증된 원본 링크만 구조화하고 제목·본문은 일반 텍스트로 전달한다."""
    text = text[:3900]
    payload: dict[str, object] = {
        "channel": user_id,
        "text": escape(text, quote=False),
        "mrkdwn": False,
        "parse": "none",
        "unfurl_links": False,
        "unfurl_media": False,
    }
    link = notion_link(url)
    if link is not None:
        title, separator, body = text.partition("\n")
        elements: list[dict[str, object]] = [
            {"type": "link", "url": link, "text": title, "style": {"bold": True}}
        ]
        if separator:
            elements.append({"type": "text", "text": "\n" + body})
        payload["blocks"] = [
            {"type": "rich_text", "elements": [{"type": "rich_text_section", "elements": elements}]}
        ]
    return payload
