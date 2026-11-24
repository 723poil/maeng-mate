"""Notion 토큰의 주인과 워크스페이스 사람 목록을 읽는다."""

import asyncio
from urllib.parse import quote

import httpx

from src.application.credentials import CredentialError, CredentialSecret
from src.application.notion_people import MAX_PEOPLE, NotionPeople, NotionPerson
from src.infrastructure.notion.errors import SourceError
from src.infrastructure.notion.read_api import NotionReadApi
from src.infrastructure.notion.values import array, record


def token_owner(payload: dict[str, object]) -> str | None:
    """개인 토큰·OAuth 토큰은 bot.owner.user가 사람이다. 내부 연결은 워크스페이스 소유라 None이다."""
    bot = payload.get("bot")
    owner = bot.get("owner") if isinstance(bot, dict) else None
    if not isinstance(owner, dict) or owner.get("type") != "user":
        return None
    user = owner.get("user")
    identifier = user.get("id") if isinstance(user, dict) else None
    return identifier if isinstance(identifier, str) and identifier else None


class NotionDirectoryReader:
    """고정 API 경로만 호출하고 이름·ID를 기록하지 않는다."""

    def __init__(self, *, transport: httpx.AsyncBaseTransport | None = None) -> None:
        """검증용 전송 계층만 주입한다."""
        self._transport = transport

    def people(self, token: CredentialSecret) -> NotionPeople:
        """Notion 오류는 공개 가능한 인증 오류 분류로 바꾼다."""
        try:
            return asyncio.run(self._read(token.value))
        except TimeoutError:
            raise CredentialError("timeout") from None
        except SourceError as error:
            raise CredentialError(
                error.code
                if error.code in {"unauthorized", "rate_limited", "timeout"}
                else "unavailable"
            ) from None
        except httpx.HTTPError:
            raise CredentialError("unavailable") from None

    async def _read(self, token: str) -> NotionPeople:
        """토큰 주인을 먼저 읽고, 사람 정보 권한이 있을 때만 목록을 모은다."""
        async with (
            asyncio.timeout(18),
            httpx.AsyncClient(
                transport=self._transport, trust_env=False, follow_redirects=False
            ) as client,
        ):
            api = NotionReadApi(token, client)
            owner = token_owner(await api.request("/users/me", None))
            people: list[NotionPerson] = []
            cursor: str | None = None
            while len(people) < MAX_PEOPLE:
                path = "/users?page_size=100"
                if cursor is not None:
                    path += "&start_cursor=" + quote(cursor, safe="")
                try:
                    page = await api.request(path, None)
                except SourceError as error:
                    # 사람 정보 권한이 없는 통합 연결은 목록 없이 직접 입력으로 대신한다.
                    if error.code == "permission_denied":
                        break
                    raise
                for item in array(page.get("results")):
                    raw = record(item)
                    identifier, name = raw.get("id"), raw.get("name")
                    # 봇과 탈퇴 사용자는 담당자로 지정될 수 없으므로 사람만 고른다.
                    if raw.get("type") != "person" or not isinstance(identifier, str):
                        continue
                    label = name.strip() if isinstance(name, str) else ""
                    people.append(NotionPerson(identifier, label or "이름 없음"))
                next_cursor = page.get("next_cursor")
                if page.get("has_more") is not True or not isinstance(next_cursor, str):
                    break
                cursor = next_cursor
            people.sort(key=lambda person: person.name)
            return NotionPeople(owner, tuple(people[:MAX_PEOPLE]))
