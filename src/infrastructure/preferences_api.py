"""설정 화면 전용 루프백 클라이언트다."""

import httpx

from src.application.errors import StatusReadError
from src.application.preferences import (
    PreferencesConflictError,
    parse_preferences,
    preferences_payload,
)
from src.domain.preferences import Preferences
from src.infrastructure.local_http import local_client, response_json


class PreferencesApi:
    """설정 화면에는 API 주소·인증만 전달한다."""

    def __init__(self, address: str, token: str, *, channels: tuple[str, ...] = ("macos",)) -> None:
        """검증된 공통 API 환경을 받는다."""
        self._address = address
        self._token = token
        self._channels = channels

    def _request(self, value: Preferences | None) -> Preferences:
        """응답 불명확 시 쓰기를 반복하지 않고 화면에 확인을 요청한다."""
        try:
            with local_client(self._address, self._token, timeout=3) as client:
                response = (
                    client.get("/v1/preferences")
                    if value is None
                    else client.put("/v1/preferences", json=preferences_payload(value))
                )
                if response.status_code == 409:
                    raise PreferencesConflictError(
                        "다른 설정이 저장되었습니다. 다시 불러와 주세요."
                    )
                response.raise_for_status()
                raw: object = response_json(response, max_bytes=300000)
                return parse_preferences(raw, self._channels)
        except (ValueError, httpx.HTTPError):
            raise StatusReadError("설정 연결을 확인한 뒤 다시 불러와 주세요.") from None

    def read(self) -> Preferences:
        """서버의 확정 설정만 읽는다."""
        return self._request(None)

    def save(self, value: Preferences) -> Preferences:
        """선택과 버전을 한 요청으로 저장한다."""
        return self._request(value)
