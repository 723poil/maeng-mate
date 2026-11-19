"""알림 큐는 인증된 명시적 POST로만 변경되는지 검증한다."""

from datetime import UTC, datetime
from pathlib import Path

import httpx


async def test_notification_claim_authorize_ack_are_authenticated(tmp_path: Path) -> None:
    """읽기 조회는 알림을 소비하지 않고 각 실행권은 한 번만 가져온다."""
    from src.infrastructure.sqlite.connection import Database
    from src.infrastructure.sqlite.notifications import SQLiteNotifications
    from src.infrastructure.sqlite.schema import migrate
    from src.presentation.http import create_app

    def now() -> datetime:
        """테스트 시점을 고정한다."""
        return datetime(2026, 11, 16, 0, tzinfo=UTC)

    db = Database(str(tmp_path / "work.sqlite3"))
    await migrate(db)
    with db.transaction() as connection:
        connection.execute(
            "INSERT INTO notification_jobs(id,logical_key,kind,title,body,due_at,expires_at,state) VALUES('fake','fake','calendar','새 일정','테스트',?,?, 'pending')",
            (now().timestamp(), now().timestamp() + 3600),
        )
    app = create_app("fake-token", lambda: {}, notification_store=SQLiteNotifications(db, now=now))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://local"
    ) as client:
        assert (await client.post("/v1/notifications/claim")).status_code == 401
        headers = {"Authorization": "Bearer fake-token"}
        assert (await client.get("/v1/status", headers=headers)).json() == {}
        response = await client.post("/v1/notifications/claim", headers=headers)
        job = response.json()["jobs"][0]
        assert (await client.post("/v1/notifications/claim", headers=headers)).json() == {
            "jobs": []
        }
        command = {"id": job["id"], "token": job["token"]}
        assert (
            await client.post("/v1/notifications/authorize", headers=headers, json=command)
        ).json() == {"authorized": True}
        assert (
            await client.post(
                "/v1/notifications/ack", headers=headers, json={**command, "result": "requested"}
            )
        ).status_code == 200
        assert (
            await client.post("/v1/notifications/authorize", headers=headers, json=command)
        ).json() == {"authorized": False}
    assert await db.fetch("SELECT state FROM notification_jobs") == [{"state": "requested"}]
