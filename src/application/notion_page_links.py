"""기존 Notion 소스의 페이지 이동 주소만 관리하는 계약이다."""

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Literal, Protocol

from src.application.notion_links import notion_link


@dataclass(frozen=True)
class NotionPageLink:
    """수집 연결은 고정하고 이동 주소만 바꿀 수 있는 표시 항목이다."""

    source_id: str = field(repr=False)
    section: Literal["tasks", "qa", "meetings"]
    label: str
    url: str | None = field(repr=False)
    summary: str = field(default="", repr=False)


@dataclass(frozen=True)
class StoredPageLinks:
    """한 버전으로 저장하는 페이지 주소의 덮어쓰기 값이다."""

    revision: int
    links: Mapping[str, str | None] = field(repr=False)


@dataclass(frozen=True)
class NotionPageLinksState:
    """현재 소스에 한해 편집할 수 있는 주소와 저장 버전이다."""

    revision: int
    sources: tuple[NotionPageLink, ...]


class PageLinksError(Exception):
    """주소 원문 없이 입력·동시 변경을 구분한다."""

    def __init__(self, code: str) -> None:
        """알 수 없는 오류 문자열을 외부에 노출하지 않는다."""
        self.code = code if code in {"invalid", "conflict"} else "storage"
        super().__init__(self.code)


class PageLinksStore(Protocol):
    """수집 스냅샷과 분리한 이동 주소 저장 포트다."""

    def read(self) -> StoredPageLinks | None:
        """미저장 상태만 None으로 구분한다."""
        ...

    def save(self, value: StoredPageLinks) -> StoredPageLinks:
        """버전이 맞을 때만 전체 주소를 원자 저장한다."""
        ...


class NotionPageLinksClient(Protocol):
    """화면은 소스나 파일을 직접 만들지 않고 주소만 편집한다."""

    def read(self) -> NotionPageLinksState:
        """현재 소스·표시 이름·주소를 읽는다."""
        ...

    def save(self, revision: int, links: Mapping[str, str | None]) -> NotionPageLinksState:
        """현재 소스의 주소만 조건부 저장한다."""
        ...


def page_url(value: object) -> str | None:
    """빈 주소는 이동 끄기로 처리하고 Notion HTTPS만 허용한다."""
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    if not isinstance(value, str) or len(value) > 4096:
        raise PageLinksError("invalid")
    result = notion_link(value.strip())
    if result is None:
        raise PageLinksError("invalid")
    return result


class NotionPageLinksService:
    """고정된 수집 소스와 기본 주소에 표시용 덮어쓰기만 적용한다."""

    def __init__(self, store: PageLinksStore, sources: tuple[NotionPageLink, ...]) -> None:
        """실행 설정에서 검증한 소스만 편집 대상으로 주입한다."""
        self._store, self._sources = store, sources

    def read(self) -> NotionPageLinksState:
        """저장 오류를 기본 주소로 숨기지 않고 미저장일 때만 파일 값을 사용한다."""
        stored = self._store.read()
        links = stored.links if stored is not None else {}
        return NotionPageLinksState(
            stored.revision if stored is not None else 0,
            tuple(
                NotionPageLink(
                    item.source_id,
                    item.section,
                    item.label,
                    links.get(item.source_id, item.url),
                    item.summary,
                )
                for item in self._sources
            ),
        )

    def save(self, revision: int, links: Mapping[str, str | None]) -> NotionPageLinksState:
        """소스 추가·삭제·속성 변경 없이 현재 주소만 저장한다."""
        if set(links) != {item.source_id for item in self._sources}:
            raise PageLinksError("conflict")
        cleaned = {key: page_url(value) for key, value in links.items()}
        current = self._store.read()
        # 현재 설정에서 잠시 빠진 소스의 주소도 삭제하지 않아 설정 복원 시 보존한다.
        merged = {**(current.links if current else {}), **cleaned}
        self._store.save(StoredPageLinks(revision, merged))
        return self.read()


def page_links_payload(state: NotionPageLinksState) -> dict[str, object]:
    """비밀값·회사 규칙 없이 이동 주소와 읽기용 속성 설명만 전달한다."""
    return {
        "revision": state.revision,
        "sources": [
            {
                "sourceId": item.source_id,
                "section": item.section,
                "label": item.label,
                "url": item.url,
                "summary": item.summary,
            }
            for item in state.sources
        ],
    }


def parse_page_links_state(value: object) -> NotionPageLinksState:
    """로컬 응답도 고정된 키·타입·중복 여부를 검사한다."""
    if not isinstance(value, dict) or set(value) != {"revision", "sources"}:
        raise PageLinksError("invalid")
    revision, sources = value["revision"], value["sources"]
    if type(revision) is not int or revision < 0 or not isinstance(sources, list):
        raise PageLinksError("invalid")
    result: list[NotionPageLink] = []
    seen: set[str] = set()
    for item in sources:
        required = {"sourceId", "section", "label", "url"}
        if (
            not isinstance(item, dict)
            or not required <= set(item)
            or set(item) - required - {"summary"}
        ):
            raise PageLinksError("invalid")
        identifier, section, label = item["sourceId"], item["section"], item["label"]
        if (
            not isinstance(identifier, str)
            or not identifier
            or identifier in seen
            or section not in ("tasks", "qa", "meetings")
            or not isinstance(label, str)
        ):
            raise PageLinksError("invalid")
        seen.add(identifier)
        kind: Literal["tasks", "qa", "meetings"] = (
            "qa" if section == "qa" else "meetings" if section == "meetings" else "tasks"
        )
        summary = item.get("summary", "")
        if not isinstance(summary, str) or len(summary) > 8192:
            raise PageLinksError("invalid")
        result.append(NotionPageLink(identifier, kind, label, page_url(item["url"]), summary))
    return NotionPageLinksState(revision, tuple(result))
