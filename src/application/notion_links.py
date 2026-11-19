"""로컬 화면에서 열 수 있는 Notion 링크를 검증한다."""

import re
from urllib.parse import urlsplit, urlunsplit


def notion_link(value: object) -> str | None:
    """Notion HTTPS를 검증하고 기존 웹 페이지 링크를 공통 앱 주소로 맞춘다."""
    if not isinstance(value, str) or any(ord(char) < 32 or ord(char) == 127 for char in value):
        return None
    try:
        parsed = urlsplit(value)
        host = parsed.hostname or ""
        allowed = host in {"notion.so", "notion.site", "app.notion.com"} or host.endswith(
            (".notion.so", ".notion.site")
        )
        if (
            parsed.scheme == "https"
            and allowed
            and parsed.username is None
            and parsed.password is None
            and parsed.port in {None, 443}
        ):
            if host == "app.notion.com" and parsed.path.startswith("/p/"):
                return value
            page = re.search(
                r"(?<![0-9a-fA-F])([0-9a-fA-F]{32}|[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12})$",
                parsed.path,
            )
            if page is not None:
                # 업무·QA·회의 모두 페이지 ID를 사용하며 데이터 원천 ID로 대체하지 않는다.
                identifier = page.group(1).replace("-", "").lower()
                return urlunsplit(
                    ("https", "app.notion.com", "/p/" + identifier, parsed.query, parsed.fragment)
                )
            return value
    except ValueError:
        pass
    return None
