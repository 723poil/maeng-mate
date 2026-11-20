"""Cocoa 수집 설정이 사용하는 인증된 루프백 클라이언트다."""

from uuid import UUID

import httpx

from src.application.errors import SettingsInputRejected
from src.application.source_settings import (
    CollectionDraft,
    SourceSchema,
    SourceSettingsError,
    SourceSettingsState,
    draft_payload,
    parse_active_display,
    parse_schemas,
    parse_source_draft,
)
from src.application.work_settings import parse_categories
from src.infrastructure.api_decoding import json_object, strings
from src.infrastructure.local_http import local_client, response_json

PATH = "/v1/source-settings"


class SourceSettingsApi:
    """외부 Notion 토큰과 데이터베이스에 직접 접근하지 않는다."""

    def __init__(self, address: str, token: str) -> None:
        """앱 내부 주소와 관리자 인증만 받는다."""
        self._address = address
        self._token = token

    def _request(self, method: str, path: str, data: dict[str, object] | None = None) -> object:
        """자동으로 요청을 재전송하지 않으며 원본 Notion 오류를 표시하지 않는다."""
        try:
            with local_client(self._address, self._token, timeout=25) as client:
                response = client.request(method, path, json=data)
                raw: object = response_json(response, max_bytes=2 * 1024 * 1024)
                if response.status_code in {409, 422, 503} and isinstance(raw, dict):
                    message = raw.get("detail")
                    if isinstance(message, str):
                        if method == "PUT" and response.status_code == 422:
                            raise SettingsInputRejected(message)
                        raise SourceSettingsError(message)
                response.raise_for_status()
                return raw
        except (SourceSettingsError, SettingsInputRejected):
            raise
        except (httpx.HTTPError, ValueError):
            raise SourceSettingsError("수집 설정의 연결을 확인하세요.") from None

    def _state(self, raw: object) -> SourceSettingsState:
        """응답의 버전과 재시작 상태를 검증한다."""
        if not isinstance(raw, dict) or type(raw.get("restartRequired")) is not bool:
            raise SourceSettingsError("수집 설정 응답을 읽지 못했습니다.")
        return SourceSettingsState(
            parse_source_draft(raw.get("settings")),
            raw["restartRequired"],
            parse_active_display(raw.get("activeDisplay", [])),
            parse_categories(raw.get("categories", [])),
        )

    def read(self) -> SourceSettingsState:
        """저장된 목록만 읽는다."""
        return self._state(self._request("GET", PATH))

    def observed_choices(self, category_id: str) -> dict[str, tuple[str, ...]]:
        """규칙 편집에 쓰는 원본 선택값을 칼럼 ID별로 읽는다."""
        try:
            path = f"{PATH}/categories/{UUID(category_id)}/observed-choices"
            raw = json_object(self._request("GET", path))
            return {str(UUID(key)): strings(values) for key, values in raw.items()}
        except SourceSettingsError:
            raise
        except ValueError:
            raise SourceSettingsError("수집한 선택값을 읽지 못했습니다.") from None

    def save(self, value: CollectionDraft) -> SourceSettingsState:
        """다른 설정과 충돌하지 않도록 읽은 버전을 전달한다."""
        return self._state(self._request("PUT", PATH, draft_payload(value)))

    def inspect(self, provider: str, reference: str, *, rounds: bool) -> tuple[SourceSchema, ...]:
        """지정한 주소의 칼럼을 한 번 조회한다."""
        raw = self._request(
            "POST",
            PATH + "/columns",
            {"provider": provider, "reference": reference, "rounds": rounds},
        )
        if not isinstance(raw, dict):
            raise SourceSettingsError("칼럼 응답을 읽지 못했습니다.")
        try:
            return parse_schemas(raw.get("schemas"))
        except (ValueError, KeyError):
            raise SourceSettingsError("칼럼 응답을 읽지 못했습니다.") from None
