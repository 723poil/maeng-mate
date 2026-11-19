"""페이지 링크 편집을 공통 업무 설정 JSON의 pageUrl에 연결한다."""

from hashlib import sha256
from threading import Lock
from types import MappingProxyType

from src.application.notion_page_links import PageLinksError, StoredPageLinks
from src.application.work_settings import Settings, WorkSettingsDocument, WorkSettingsError
from src.infrastructure.sqlite.connection import Database
from src.infrastructure.sqlite.work_settings import (
    SQLiteWorkSettings,
    read_work_settings_row,
    validate_work_document,
)


class SQLiteNotionPageLinks:
    """기존 화면 계약은 유지하며 별도의 링크 문서에는 쓰지 않는다."""

    def __init__(self, database: Database) -> None:
        """공통 설정 저장소에 같은 DB를 주입한다."""
        self._database = database
        self._store = SQLiteWorkSettings(database)
        self._lock = Lock()
        self._cached: tuple[tuple[int, bool, bytes], StoredPageLinks] | None = None

    def read(self) -> StoredPageLinks | None:
        """소스 매핑과 함께 저장된 이동 주소를 같은 버전으로 읽는다."""
        with self._lock, self._database.transaction(read_only=True) as connection:
            row = read_work_settings_row(connection)
            if row is None:
                self._cached = None
                return None
            revision, encoded = row
            # 같은 버전에서 초기 문서가 이전되거나 DB가 교체돼도 현재 원문과 대조한다.
            digest = sha256(encoded.encode() if isinstance(encoded, str) else encoded).digest()
            # JSON 디코더는 TEXT와 BLOB의 인코딩을 다르게 처리할 수 있다.
            key = (revision, isinstance(encoded, str), digest)
            if self._cached is not None and self._cached[0] == key:
                return self._cached[1]
            links = stored_page_links(*validate_work_document(revision, encoded))
            self._cached = key, links
            return links

    def save(self, value: StoredPageLinks) -> StoredPageLinks:
        """주소만 원자 저장하고 다른 설정과 충돌하면 전체 변경을 거부한다."""
        try:
            saved = self._store.save_links(value.revision, value.links)
        except WorkSettingsError as error:
            raise PageLinksError(error.code) from None
        return stored_page_links(*saved)


def stored_page_links(document: WorkSettingsDocument, settings: Settings) -> StoredPageLinks:
    """조회와 저장 응답에 같은 버전과 소스별 주소 목록을 사용한다."""
    return StoredPageLinks(
        document.revision,
        MappingProxyType(
            {
                source.source_id: settings.source_pages.get(source.source_id)
                for source in settings.sources
            }
        ),
    )
