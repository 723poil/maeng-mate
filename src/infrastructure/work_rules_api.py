"""근무 규칙 편집용 루프백 클라이언트다."""

import httpx

from src.application.errors import StatusReadError
from src.application.work_rules_settings import (
    WorkRulesDocument,
    WorkRulesState,
    document_payload,
    parse_document,
)
from src.infrastructure.local_http import local_client


class WorkRulesApi:
    """메뉴바가 파일 위치·저장 구현을 알 필요가 없게 한다."""

    def __init__(self, address: str, token: str) -> None:
        """검증한 앱 내부 주소와 관리 키를 주입한다."""
        self._address = address
        self._token = token

    def read(self) -> WorkRulesState:
        """편집값만 조회한다."""
        return self._request(None)

    def save(self, value: WorkRulesDocument) -> WorkRulesState:
        """확정 버전의 규칙을 한 번 저장한다."""
        return self._request(value)

    def _request(self, value: WorkRulesDocument | None) -> WorkRulesState:
        """저장 요청은 자동 반복하지 않고 응답 타입을 검사한다."""
        try:
            with local_client(self._address, self._token, timeout=3) as client:
                response = (
                    client.get("/v1/work-rules")
                    if value is None
                    else client.put("/v1/work-rules", json=document_payload(value))
                )
                response.raise_for_status()
                data: object = response.json()
                if not isinstance(data, dict) or type(data.get("restartRequired")) is not bool:
                    raise ValueError
                return WorkRulesState(parse_document(data.get("settings")), data["restartRequired"])
        except (ValueError, httpx.HTTPError):
            raise StatusReadError("근무 규칙의 연결과 입력을 확인하세요.") from None
