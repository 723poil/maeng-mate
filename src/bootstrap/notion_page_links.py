"""현재 Notion 소스와 표시용 주소 저장을 연결한다."""

from collections import Counter
from typing import Literal

from src.application.notion_page_links import NotionPageLink, NotionPageLinksService, page_url
from src.application.notion_source_summary import source_summary
from src.application.work_settings import Settings
from src.infrastructure.sqlite.connection import Database
from src.infrastructure.sqlite.notion_page_links import SQLiteNotionPageLinks


def build_page_links(database: Database, settings: Settings | None) -> NotionPageLinksService:
    """수집 종류·순서는 유지하며 업무·QA·회의의 링크만 편집 대상으로 만든다."""
    definitions: list[tuple[str, Literal["tasks", "qa", "meetings"], str | None]] = []
    summaries: dict[str, str] = {}
    if settings is not None:
        for source in settings.sources:
            if source.kind not in {"task", "meeting"}:
                continue
            section: Literal["tasks", "qa", "meetings"] = (
                "meetings" if source.kind == "meeting" else source.section
            )
            definitions.append(
                (source.source_id, section, page_url(settings.source_pages.get(source.source_id)))
            )
            summaries[source.source_id] = source_summary(source)
    counts = Counter(section for _, section, _ in definitions)
    index: Counter[str] = Counter()
    result: list[NotionPageLink] = []
    for identifier, section, url in definitions:
        index[section] += 1
        label = {"tasks": "업무", "qa": "QA", "meetings": "회의"}[section]
        if counts[section] > 1:
            label += f" {index[section]}"
        result.append(NotionPageLink(identifier, section, label, url, summaries[identifier]))
    return NotionPageLinksService(SQLiteNotionPageLinks(database), tuple(result))
