"""독립된 근무 규칙 설정 API가 같은 파일에 저장할 때의 버전 계약을 확인한다."""

import asyncio
from dataclasses import replace
from pathlib import Path

import httpx

from src.application.work_rules_settings import (
    WorkRulesDocument,
    WorkRulesService,
    document_payload,
)
from src.domain.daily_work import DEFAULT_WORK_RULES, WorkRules
from src.infrastructure.work_rules_file import WorkRulesFile
from src.presentation.http import create_app
from tests.fixtures.preferences import enable_notifications


async def test_concurrent_writers_share_revision_and_keep_legacy_defaults(tmp_path: Path) -> None:
    """같은 버전의 저장은 한 번만 성공하고 충돌한 작성자는 최신 값으로 다시 저장할 수 있다."""
    path = tmp_path / "private" / "work-rules.json"
    legacy = replace(DEFAULT_WORK_RULES, daily_minutes=450)

    def client() -> httpx.AsyncClient:
        """같은 파일을 공유하되 서비스·저장소 인스턴스가 독립인 관리자 클라이언트다."""
        service = WorkRulesService(WorkRulesFile(path), legacy)
        app = create_app("fixture", lambda: {}, work_rules_service=service)
        return httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
            headers={"Authorization": "Bearer fixture"},
        )

    route = "/v1/work-rules"
    async with client() as first, client() as second:
        initial = await first.get(route)
        assert initial.json() == {
            "settings": document_payload(WorkRulesDocument(0, legacy)),
            "restartRequired": False,
        }
        assert not path.parent.exists()
        drafts = [
            document_payload(WorkRulesDocument(0, replace(legacy, daily_minutes=n)))
            for n in (460, 470)
        ]
        responses = await asyncio.gather(
            first.put(route, json=drafts[0]), second.put(route, json=drafts[1])
        )
        assert sorted(r.status_code for r in responses) == [200, 409]
        winner = next(r.json() for r in responses if r.status_code == 200)
        assert winner["settings"]["revision"] == 1 and winner["restartRequired"] is False
        assert (await second.get(route)).json() == winner
        saved = await second.put(route, json=document_payload(WorkRulesDocument(1, legacy)))
        assert saved.status_code == 200
        assert saved.json() == {
            "settings": document_payload(WorkRulesDocument(2, legacy)),
            "restartRequired": False,
        }
        assert path.stat().st_mode & 0o777 == 0o600


async def test_saved_rules_recalculate_today_and_pending_reminders(tmp_path: Path) -> None:
    """규칙 저장은 오늘 기록과 예약만 갱신하고 실제 입력·지난 기록·미기록일을 보존한다."""
    from datetime import datetime, time, timedelta

    from src.application.day_records import DayService
    from src.application.work_records import WorkRecordsService
    from src.domain.daily_work import DayRecord, FlexibleDeparturePolicy
    from src.domain.work_dates import SEOUL
    from src.infrastructure.sqlite.connection import Database
    from src.infrastructure.sqlite.day_records import SQLiteDayStore
    from src.infrastructure.sqlite.schema import migrate

    now = datetime(2026, 11, 19, 9, tzinfo=SEOUL)
    today = now.date()
    database = Database(str(tmp_path / "attendance.sqlite3"))
    await migrate(database)
    enable_notifications(database, "departure_17", "departure_4")
    policy = FlexibleDeparturePolicy()
    writer = SQLiteDayStore(database, now=lambda: now, policy=policy)
    service = WorkRulesService(
        WorkRulesFile(tmp_path / "work-rules.json", apply_today=writer.apply_today_rules), None
    )
    store = SQLiteDayStore(
        database, now=lambda: now, policy=policy, current_rules=service.current_rules
    )
    app = create_app(
        "fixture",
        lambda: {},
        work_rules_service=service,
        day_service=DayService(store, policy, now=lambda: now),
        work_records_service=WorkRecordsService(store, now=lambda: now, policy=policy),
    )
    previous = await store.correct(
        DayRecord(today - timedelta(days=1), attendance=now - timedelta(days=1)), 0
    )
    lunch = replace(DEFAULT_WORK_RULES, lunch_start=time(12), lunch_end=time(13))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
        headers={"Authorization": "Bearer fixture"},
    ) as client:

        async def save_rules(revision: int, rules: WorkRules) -> None:
            """근무 규칙 편집 화면과 같은 API로 저장해 즉시 적용 응답을 확인한다."""
            response = await client.put(
                "/v1/work-rules", json=document_payload(WorkRulesDocument(revision, rules))
            )
            assert response.status_code == 200 and response.json()["restartRequired"] is False

        await save_rules(0, DEFAULT_WORK_RULES)
        assert (await client.get("/v1/day")).json()["revision"] == 0
        body = {
            "date": today.isoformat(),
            "revision": 0,
            "attendanceTime": "08:00",
            "leaveType": "none",
            "plan": "가상 계획",
        }
        saved = (await client.put("/v1/day", json=body)).json()
        assert datetime.fromisoformat(saved["expectedDepartureAt"]).astimezone(SEOUL).hour == 16
        original = await store.read(today)
        await save_rules(1, lunch)
        revised = await store.read(today)
        assert revised == replace(original, revision=2, rules=lunch)
        expected = now.replace(hour=17)
        current = (await client.get("/v1/day")).json()
        assert datetime.fromisoformat(current["expectedDepartureAt"]) == expected
        assert current["lunchMinutes"] == 60 and current["previewRules"] == lunch.payload()
        detail = (await client.get(f"/v1/work-records/{today}")).json()
        assert detail["expectedDepartureAt"] == current["expectedDepartureAt"]
        jobs = await database.fetch(
            "SELECT revision,due_at FROM notification_jobs WHERE state='pending'"
        )
        assert {row["revision"] for row in jobs} == {2}
        assert {row["due_at"] for row in jobs} == {
            (expected - timedelta(minutes=n)).timestamp() for n in (17, 4)
        }
        assert (await client.put("/v1/day", json={**body, "revision": 1})).status_code == 409
        await save_rules(2, lunch)
        assert (await store.read(today)).revision == 2
        finished = await store.correct(replace(revised, checkout_at=now, overtime=True), 2)
        await save_rules(3, DEFAULT_WORK_RULES)
        assert await store.read(today) == replace(finished, revision=4, rules=DEFAULT_WORK_RULES)
        assert not await database.fetch("SELECT id FROM notification_jobs WHERE state='pending'")
        assert await store.read(previous.day) == previous
