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
from src.domain.daily_work import DEFAULT_WORK_RULES
from src.infrastructure.work_rules_file import WorkRulesFile
from src.presentation.http import create_app


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
        assert winner["settings"]["revision"] == 1 and winner["restartRequired"] is True
        assert (await second.get(route)).json() == winner
        saved = await second.put(route, json=document_payload(WorkRulesDocument(1, legacy)))
        assert saved.status_code == 200
        assert saved.json() == {
            "settings": document_payload(WorkRulesDocument(2, legacy)),
            "restartRequired": False,
        }
        assert path.stat().st_mode & 0o777 == 0o600
