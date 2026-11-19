"""페이지 주소가 같은 버전의 현재 SQLite 문서와 일치하는지 검증한다."""

import json

import pytest

from src.application.errors import SnapshotError
from src.infrastructure.sqlite.connection import Database
from src.infrastructure.sqlite.notion_page_links import SQLiteNotionPageLinks
from src.infrastructure.sqlite.work_settings import SQLiteWorkSettings


async def test_link_read_revalidates_replaced_document_with_same_revision(db: Database) -> None:
    """같은 버전에서 주소·매핑이 바뀌거나 문서가 사라져도 이전 링크를 반환하지 않는다."""
    source = {
        "kind": "task",
        "dataSourceId": "fixture",
        "properties": {"title": "t"},
        "flexible": True,
    }
    payload = {"version": 1, "timezone": "Asia/Seoul", "sources": [source]}
    SQLiteWorkSettings(db).initialize(payload)
    links = SQLiteNotionPageLinks(db)
    initial = links.read()
    assert initial is not None and initial.links == {"fixture": None}
    assert links.read() == initial

    source["pageUrl"] = "https://www.notion.so/example"
    with db.transaction() as connection:
        connection.execute("UPDATE work_settings SET payload=? WHERE id=1", (json.dumps(payload),))
    changed = links.read()
    assert changed is not None and changed.revision == initial.revision
    assert changed.links == {"fixture": source["pageUrl"]}

    # 주소 외의 필드가 손상돼도 마지막 정상 결과로 오류를 덮지 않는다.
    with db.transaction() as connection:
        connection.execute(
            "UPDATE work_settings SET payload=json_set(payload,'$.sources[0].kind','invalid')"
        )
    with pytest.raises(SnapshotError):
        links.read()
    with db.transaction() as connection:
        connection.execute("DELETE FROM work_settings WHERE id=1")
    assert links.read() is None
