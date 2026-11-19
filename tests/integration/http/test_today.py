"""저장소 종류와 무관한 개인 조회 HTTP 경계를 검증한다."""

import httpx
import pytest

from src.application.errors import QueryReadError
from src.presentation.http import create_app

TOKEN = "fake-local-api-token-" * 3


async def today(date: str | None) -> dict[str, object]:
    """요청 날짜가 조회 계약에 전달되는지 확인할 값을 반환한다."""
    return {"date": date, "tasks": [], "meetings": [], "stale": True}


async def test_today_is_authenticated_and_forwards_date() -> None:
    """본인 인증 뒤 날짜만 조회에 전달한다."""
    app = create_app(TOKEN, lambda: {}, today)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://local"
    ) as client:
        assert (await client.get("/v1/today")).status_code == 401
        response = await client.get(
            "/v1/today?date=2026-11-13", headers={"Authorization": f"Bearer {TOKEN}"}
        )
        assert response.status_code == 200
        assert response.json()["date"] == "2026-11-13"
        assert response.headers["Cache-Control"] == "no-store"
        assert TOKEN not in response.text


@pytest.mark.parametrize("date", ["2026-02-30", "20261113", "secret-invalid-input"])
async def test_invalid_date_has_safe_error(date: str) -> None:
    """형식 오류를 조회 전에 거부하고 입력 원문을 응답에 복사하지 않는다."""
    app = create_app(TOKEN, lambda: {}, today)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://local"
    ) as client:
        response = await client.get(
            "/v1/today", params={"date": date}, headers={"Authorization": f"Bearer {TOKEN}"}
        )
        assert response.status_code == 400
        assert date not in response.text


async def test_unconfigured_query_and_storage_failure_are_explicit() -> None:
    """설정 누락과 읽기 실패를 정상적인 빈 업무로 표현하지 않는다."""

    async def failed(date: str | None) -> dict[str, object]:
        """저장 경계가 변환한 안전한 오류를 발생시킨다."""
        raise QueryReadError()

    for query in (None, failed):
        app = create_app(TOKEN, lambda: {}, query)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://local"
        ) as client:
            response = await client.get("/v1/today", headers={"Authorization": f"Bearer {TOKEN}"})
            assert response.status_code == 503
            assert TOKEN not in response.text
