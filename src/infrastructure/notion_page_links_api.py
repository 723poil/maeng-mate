"""이동 주소 관리에만 사용하는 인증된 루프백 클라이언트다."""

from collections.abc import Mapping

import httpx

from src.application.notion_page_links import (
    NotionPageLinksState,
    PageLinksError,
    parse_page_links_state,
)
from src.infrastructure.local_http import local_client, response_json


class NotionPageLinksApi:
    """수집 매핑이나 비밀값을 전달하지 않고 기존 주소만 편집한다."""

    def __init__(self, address: str, token: str) -> None:
        """앱 내부 관리 연결만 주입한다."""
        self._address, self._token = address, token

    def read(self) -> NotionPageLinksState:
        """기존 소스의 주소·레이블을 읽는다."""
        return self._request(None)

    def save(self, revision: int, links: Mapping[str, str | None]) -> NotionPageLinksState:
        """전체 주소와 읽은 버전만 한 번 전송한다."""
        return self._request(
            {
                "revision": revision,
                "links": [{"sourceId": key, "url": url} for key, url in links.items()],
            }
        )

    def _request(self, body: dict[str, object] | None) -> NotionPageLinksState:
        """실패 원문은 숨기고 쓰기를 자동 반복하지 않는다."""
        try:
            with local_client(self._address, self._token, timeout=5) as client:
                response = (
                    client.get("/v1/notion/page-links")
                    if body is None
                    else client.put("/v1/notion/page-links", json=body)
                )
                if response.status_code != 200:
                    raise PageLinksError(
                        "conflict"
                        if response.status_code == 409
                        else "invalid"
                        if response.status_code == 422
                        else "storage"
                    )
                payload: object = response_json(response, max_bytes=1048576)
                return parse_page_links_state(payload)
        except (httpx.HTTPError, ValueError):
            raise PageLinksError("storage") from None
