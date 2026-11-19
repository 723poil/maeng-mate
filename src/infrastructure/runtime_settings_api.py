"""메뉴바의 일반·Notion 설정 편집용 루프백 어댑터다."""

import httpx

from src.application.errors import StatusReadError
from src.application.runtime_settings import RuntimeSettingsConflictError, runtime_settings_payload
from src.application.runtime_settings_service import RuntimeSettingsState, parse_runtime_state
from src.domain.runtime_settings import RuntimeSettings
from src.infrastructure.local_http import local_client, response_json


class RuntimeSettingsApi:
    """로컬 API 주소·인증만 받고 DB·프로바이더 토큰은 받지 않는다."""

    def __init__(self, address: str, token: str) -> None:
        """검증한 공통 API 연결을 주입한다."""
        self._address = address
        self._token = token

    def read(self) -> RuntimeSettingsState:
        """저장된 값과 이번 실행의 적용 상태를 조회한다."""
        return self._request(None)

    def save(self, value: RuntimeSettings) -> RuntimeSettingsState:
        """확정 버전의 설정을 한 번만 저장한다."""
        return self._request(value)

    def _request(self, value: RuntimeSettings | None) -> RuntimeSettingsState:
        """실패 원문은 숨기고 불명확한 쓰기를 자동 반복하지 않는다."""
        try:
            with local_client(self._address, self._token, timeout=3) as client:
                response = (
                    client.get("/v1/runtime-settings")
                    if value is None
                    else client.put("/v1/runtime-settings", json=runtime_settings_payload(value))
                )
                if response.status_code == 409:
                    raise RuntimeSettingsConflictError(
                        "다른 설정이 저장되었습니다. 다시 불러오세요."
                    )
                response.raise_for_status()
                return parse_runtime_state(response_json(response, max_bytes=16384))
        except (ValueError, httpx.HTTPError):
            raise StatusReadError("설정 연결과 입력을 확인하세요.") from None
