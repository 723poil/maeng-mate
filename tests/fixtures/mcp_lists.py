"""실제 원천 어댑터와 임시 SQLite를 사용하는 목록 조회 자료다."""

import json
from copy import deepcopy
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import httpx

from src.application.work_settings import Settings
from src.bootstrap.clock import now
from src.domain.category_configuration import CategoryConfiguration
from src.domain.display_columns import DisplayColumn
from src.domain.main_list import ListFilter, MainListRules
from src.domain.models import SourceMapping
from src.domain.source_display import SourceCategory, SourceDisplay
from src.infrastructure.notion.source import NotionSource
from src.infrastructure.sqlite.connection import Database
from src.infrastructure.sqlite.schema import migrate
from src.infrastructure.sqlite.snapshots import SQLiteSnapshots
from tests.fixtures.notion import page, task_schema

ADMIN = "fake-list-admin-" * 4
RAW_TEXT = "원형  공백\n" + "가" * 300


def settings() -> Settings:
    """담당자 연결 유무와 화면 숨김 조건이 서로 다른 가상 목록을 만든다."""
    category = SourceCategory(
        "list-category",
        "가상 목록",
        CategoryConfiguration(
            main_list=MainListRules(exclusions=(ListFilter("title", "contains", "가상"),))
        ),
    )
    column = DisplayColumn("note", "설명", "rich_text", "n")
    source = SourceMapping(
        "task",
        "list-scoped",
        {"title": "t", "status": "s", "assignees": "p", "dueDate": "d"},
        flexible=True,
        stored_columns=(
            column,
            DisplayColumn("amount", "수량", "number", "num"),
            DisplayColumn("checked", "확인", "checkbox", "chk"),
            DisplayColumn("tags", "선택", "multi_select", "multi"),
        ),
    )
    free = replace(
        source, source_id="list-free", properties={"title": "t", "status": "s", "dueDate": "d"}
    )
    hidden = replace(source, source_id="list-ungranted")
    sources = (source, free, hidden)
    return Settings(
        sources,
        display=tuple(
            SourceDisplay(s.source_id, "task", "tasks", category=category) for s in sources
        ),
    )


async def prepare(path: Path) -> tuple[Database, Settings]:
    """원천 응답은 가상화하고 수집·정규화·저장 구현은 그대로 실행한다."""
    database = Database(str(path))
    await migrate(database)
    configured = settings()

    async def respond(request: httpx.Request) -> httpx.Response:
        """본인·타인·담당자 없는 세 행을 담당자 필터 없이 제공한다."""
        assert request.url.host == "api.notion.com"
        if request.method == "GET":
            return httpx.Response(200, json=task_schema())
        body: object = json.loads(request.content)
        assert isinstance(body, dict) and "filter" not in body
        pages = []
        for index, people in enumerate(([{"id": "fixture-me"}], [{"id": "fixture-other"}], [])):
            value = deepcopy(page())
            value["id"] = f"item-{index}"
            props = value["properties"]
            assert isinstance(props, dict)
            props["People"] = {"id": "p", "type": "people", "people": people}
            props["Note"] = {
                "id": "n",
                "type": "rich_text",
                "rich_text": [{"plain_text": RAW_TEXT}],
            }
            props["Number"] = {"id": "num", "type": "number", "number": 0}
            props["Check"] = {"id": "chk", "type": "checkbox", "checkbox": False}
            props["Tags"] = {
                "id": "multi",
                "type": "multi_select",
                "multi_select": [{"name": "A"}, {"name": "B"}],
            }
            pages.append(value)
        return httpx.Response(
            200, json={"object": "list", "results": pages, "has_more": False, "next_cursor": None}
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        source_reader = NotionSource("fake-source-token", client, interval=0, user_id="fixture-me")
        store = SQLiteSnapshots(database)
        for source in configured.sources:
            observed = datetime.now(UTC).isoformat()
            collected = await source_reader.collect(source, observed)
            assert len(collected.items) == 3
            run = await store.begin_run(source, observed)
            await store.complete_run(
                run, collected.items, now().isoformat(), status_options=collected.status_options
            )
    return database, configured


async def business_state(database: Database) -> dict[str, list[dict[str, object]]]:
    """인증 사용 시각을 제외한 수집·설정·근무·알림 저장 상태를 대조한다."""
    tables = (
        "item_snapshots",
        "work_source_state",
        "sync_runs",
        "sync_schedules",
        "work_settings",
        "runtime_settings",
        "app_preferences",
        "collection_preferences",
        "day_records",
        "day_record_revisions",
        "notification_jobs",
        "notification_deliveries",
        "task_changes",
        "source_alert_baselines",
        "source_alert_events",
    )
    return {
        table: await database.fetch(f"SELECT * FROM {table} ORDER BY rowid") for table in tables
    }
