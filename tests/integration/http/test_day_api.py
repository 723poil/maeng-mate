"""근무 기록 API의 인증·수정·민감 입력 오류 처리를 검증한다."""

import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime, time
from pathlib import Path

import httpx


async def test_day_api_authenticated_edit_conflict_and_validation(tmp_path: Path) -> None:
    """로컬 기록만 저장하고 낡은 버전과 잘못된 입력은 원문 없이 거부한다."""
    from src.application.day_records import DayService
    from src.domain.daily_work import FlexibleDeparturePolicy, WorkRules
    from src.infrastructure.sqlite.connection import Database
    from src.infrastructure.sqlite.day_records import SQLiteDayStore
    from src.infrastructure.sqlite.schema import migrate
    from src.presentation.http import create_app

    def now() -> datetime:
        """날짜 전환 없이 API 입력 검증 시각을 고정한다."""
        return datetime(2026, 11, 16, 0, tzinfo=UTC)

    database = Database(str(tmp_path / "work.sqlite3"))
    await migrate(database)
    service = DayService(
        SQLiteDayStore(
            database,
            now=now,
            current_rules=lambda: WorkRules(
                480, 240, 120, time(12), time(13), time(0), time(23, 59)
            ),
        ),
        FlexibleDeparturePolicy(),
        now=now,
    )
    app = create_app("fake-token", lambda: {}, day_service=service)
    headers = {"Authorization": "Bearer fake-token"}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://local"
    ) as client:
        assert (await client.get("/v1/day")).status_code == 401
        initial = await client.get("/v1/day", headers=headers)
        assert initial.json()["revision"] == 0
        body = {
            "date": "2026-11-16",
            "revision": 0,
            "attendanceTime": "09:01",
            "leaveType": "pm_half",
            "plan": "비공개 오늘 계획",
        }

        async def chunks(data: bytes) -> AsyncIterator[bytes]:
            """길이 헤더 없이 나뉜 본문도 전체 바이트 수로 검사한다."""
            yield data[:16384]
            yield data[16384:]

        at_limit = json.dumps(body, ensure_ascii=False).encode().ljust(32768)
        saved = await client.put("/v1/day", headers=headers, content=chunks(at_limit))
        assert saved.status_code == 200
        assert saved.json()["revision"] == 1
        assert saved.json()["expectedDepartureAt"] == "2026-11-16T05:01:00+00:00"
        assert (await client.put("/v1/day", headers=headers, json=body)).status_code == 409
        invalid = await client.put(
            "/v1/day", headers=headers, json={**body, "attendanceTime": "비공개 잘못된 시간"}
        )
        assert invalid.status_code == 422
        assert "비공개" not in invalid.text
        oversized = await client.put("/v1/day", headers=headers, content=chunks(at_limit + b" "))
        assert oversized.status_code == 413
        assert (await client.get("/v1/status", headers=headers)).json() == {}
    assert len(await database.fetch("SELECT revision FROM day_record_revisions")) == 1


async def test_day_write_missing_schema_is_storage_error(tmp_path: Path) -> None:
    """DB 미준비를 사용자 입력 오류로 잘못 응답하지 않는다."""
    from src.application.day_records import DayService
    from src.domain.daily_work import FlexibleDeparturePolicy
    from src.infrastructure.sqlite.connection import Database
    from src.infrastructure.sqlite.day_records import SQLiteDayStore
    from src.presentation.http import create_app

    def now() -> datetime:
        """인증된 입력 날짜만 고정한다."""
        return datetime(2026, 11, 16, 0, tzinfo=UTC)

    service = DayService(
        SQLiteDayStore(Database(str(tmp_path / "missing.sqlite3")), now=now),
        FlexibleDeparturePolicy(),
        now=now,
    )
    app = create_app("fake-token", lambda: {}, day_service=service)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://local"
    ) as client:
        response = await client.put(
            "/v1/day",
            headers={"Authorization": "Bearer fake-token"},
            json={
                "date": "2026-11-16",
                "revision": 0,
                "attendanceTime": "09:00",
                "leaveType": "none",
                "plan": "",
            },
        )
    assert response.status_code == 503
    assert not (tmp_path / "missing.sqlite3").exists()
