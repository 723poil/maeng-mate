"""본인 인증과 안전한 상태 응답을 검증한다."""

import httpx

from src.bootstrap.environment import app_environment
from src.bootstrap.wiring import build_api


async def test_authentication_and_status() -> None:
    """잘못된 인증은 거부하고 실제 연결되지 않은 서비스를 정직하게 표시한다."""
    token = "test-secret-" * 5
    app = build_api(app_environment({"LOCAL_API_TOKEN": token, "NOTION_TOKEN": "private-notion"}))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://local"
    ) as client:
        assert (await client.get("/v1/status")).status_code == 401
        assert (
            await client.get("/v1/status", headers={"Authorization": "Bearer wrong"})
        ).status_code == 401
        response = await client.get(
            "/v1/status",
            headers={"Authorization": f"Bearer {token}", "Origin": "https://example.com"},
        )
        assert response.status_code == 200
        assert response.json()["automaticExecution"] == "disabled"
        assert "private-notion" not in response.text
        assert token not in response.text
        assert response.headers["cache-control"] == "no-store"
        assert "access-control-allow-origin" not in response.headers


async def test_periodic_status_is_authenticated_and_read_only() -> None:
    """정기 수집 집계는 인증 후에만 읽고 조회 자체로 수집하지 않는다."""
    token = "fake-api-token-" * 4
    queries: list[str] = []

    async def periodic() -> dict[str, object]:
        """구체 수집기 대신 안전한 조회 계약만 주입한다."""
        queries.append("read")
        return {
            "enabled": True,
            "running": False,
            "nextRunAt": "2026-11-16T01:00:00+00:00",
            "failedSources": 1,
        }

    app = build_api(app_environment({"LOCAL_API_TOKEN": token}), periodic_status=periodic)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://local"
    ) as client:
        assert (await client.get("/v1/status")).status_code == 401
        assert queries == []
        response = await client.get("/v1/status", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 200
    assert response.json()["automaticExecution"] == "enabled"
    assert response.json()["periodicSync"]["failedSources"] == 1
    assert queries == ["read"]
    assert token not in response.text


async def test_periodic_status_storage_failure_returns_safe_error() -> None:
    """상태 저장 실패를 비활성이나 정상 상태로 바꾸지 않는다."""
    from src.application.errors import SnapshotError

    token = "fake-api-token-" * 4

    async def periodic() -> dict[str, object]:
        """저장 실패의 원문을 외부에 전달하지 않는다."""
        raise SnapshotError("fake-private-error")

    app = build_api(app_environment({"LOCAL_API_TOKEN": token}), periodic_status=periodic)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://local"
    ) as client:
        response = await client.get("/v1/status", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 503
    assert "fake-private-error" not in response.text
