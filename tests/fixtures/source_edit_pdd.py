"""가상 표와 임시 SQLite로 AppKit 소스 편집의 저장·재조회를 확인한다."""

import asyncio
from collections.abc import Callable
from pathlib import Path
from tempfile import TemporaryDirectory
from time import monotonic, sleep

from AppKit import NSApplication, NSScrollView

from src.application.source_settings import (
    SourceColumn,
    SourceSchema,
    SourceSettingsService,
    SourceTable,
)
from src.application.work_settings import empty_work_settings, source_documents
from src.infrastructure.sqlite.connection import Database
from src.infrastructure.sqlite.schema import migrate
from src.infrastructure.sqlite.work_settings import SQLiteWorkSettings
from src.presentation.runtime_settings_controller import SettingsEditor
from src.presentation.source_settings_form import SourceSettingsForm


class Catalog:
    """표마다 다른 제목 칼럼을 제공하는 가상 조회기다."""

    def inspect(self, reference: str, *, rounds: bool) -> tuple[SourceSchema, ...]:
        """하위 표의 속성 이름으로 매핑 재사용과 교체를 구별한다."""
        return (
            SourceSchema(reference, reference, (SourceColumn("t", reference, "title"),), ("부모",)),
        )

    def tables(self) -> tuple[SourceTable, ...]:
        """주소 입력만 사용하는 시나리오의 빈 목록이다."""
        return ()


def wait_until(ready: Callable[[], bool], tick: Callable[[], None]) -> None:
    """비동기 조회가 끝나면 즉시 진행하고 실패 시 무한 대기를 막는다."""
    deadline = monotonic() + 5
    while not ready():
        assert monotonic() < deadline
        tick()
        sleep(0.01)


def verify(directory: Path) -> None:
    """실제 폼에서 편집한 네 단계의 저장 문서와 버전을 대조한다."""
    NSApplication.sharedApplication()
    database = Database(str(directory / "fixture.sqlite"))
    asyncio.run(migrate(database))
    store = SQLiteWorkSettings(database)
    original = {
        "kind": "task",
        "provider": "notion",
        "sourceId": "logical",
        "dataSourceId": "first",
        "categoryId": "00000000-0000-4000-8000-000000000045",
        "flexible": True,
        "properties": {"title": "first"},
        "roundTitleProperty": "부모",
    }
    store.initialize(
        {
            **empty_work_settings(),
            "categories": [{"id": "00000000-0000-4000-8000-000000000045", "name": "가상 분류"}],
            "sources": [original],
        }
    )
    service = SourceSettingsService(store, None, {"notion": Catalog()})
    editor = SettingsEditor(service)
    page = NSScrollView.alloc().initWithFrame_(((0, 0), (700, 600)))
    form = SourceSettingsForm(page, 600, editor, service)
    try:
        wait_until(lambda: not editor.busy, editor.tick)
        form.render()
        for revision in range(1, 5):
            if revision == 1:
                form.name.setStringValue_("별칭")
                form.mark_edited()
            elif revision in (2, 3):
                form.reference.setStringValue_("first" if revision == 2 else "second")
                form.inspect()
                wait_until(lambda: not form.busy, form.poll)
                form.mark_edited()
            else:
                form.toggle_removal()
            draft = form.draft()
            assert draft.revision == revision
            editor.save(draft)
            wait_until(lambda: not editor.busy, editor.tick)
            stored = store.read()
            assert stored and stored.revision == revision + 1, editor.error
            saved = source_documents(stored.payload)
            assert tuple(saved) == draft.sources == service.read().settings.sources
            if revision == 4:
                assert saved == []
            else:
                value = saved[0]
                expected = "second" if revision == 3 else "first"
                assert value["sourceId"] == "logical" and value["dataSourceId"] == expected
                assert value["properties"] == {"title": expected} and value["name"] == "별칭"
                assert value["parentTitleProperty"] == "부모" and "roundTitleProperty" not in value
                if revision == 1:
                    assert value["parentOrder"] == "legacy" and value["parentLabel"] is True
            form.render()
    finally:
        form.close()
        editor.close()


if __name__ == "__main__":
    with TemporaryDirectory() as directory:
        verify(Path(directory))
