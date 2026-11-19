"""페이지 링크 편집을 공통 업무 설정 JSON의 pageUrl에 연결한다."""

from src.application.notion_page_links import PageLinksError, StoredPageLinks
from src.application.work_settings import WorkSettingsDocument, WorkSettingsError, parse_settings
from src.infrastructure.sqlite.connection import Database
from src.infrastructure.sqlite.work_settings import SQLiteWorkSettings


class SQLiteNotionPageLinks:
    """기존 화면 계약은 유지하며 별도의 링크 문서에는 쓰지 않는다."""

    def __init__(self, database: Database) -> None:
        """공통 설정 저장소에 같은 DB를 주입한다."""
        self._store = SQLiteWorkSettings(database)

    def read(self) -> StoredPageLinks | None:
        """소스 매핑과 함께 저장된 이동 주소를 같은 버전으로 읽는다."""
        document = self._store.read()
        if document is None:
            return None
        return stored_page_links(document)

    def save(self, value: StoredPageLinks) -> StoredPageLinks:
        """주소만 원자 저장하고 다른 설정과 충돌하면 전체 변경을 거부한다."""
        try:
            document = self._store.save_links(value.revision, value.links)
        except WorkSettingsError as error:
            raise PageLinksError(error.code) from None
        return stored_page_links(document)


def stored_page_links(document: WorkSettingsDocument) -> StoredPageLinks:
    """조회와 저장 응답에 같은 버전과 소스별 주소 목록을 사용한다."""
    settings = parse_settings(document.payload)
    return StoredPageLinks(
        document.revision,
        {
            source.source_id: settings.source_pages.get(source.source_id)
            for source in settings.sources
        },
    )
