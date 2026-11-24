"""Notion 토큰 주인과 워크스페이스 사람 목록으로 본인 ID를 고르는 계약이다."""

from dataclasses import dataclass, field
from typing import Protocol

from src.application.credentials import CredentialSecret

# 설정 화면의 선택 메뉴에 담을 사람 수 상한이다.
MAX_PEOPLE = 500


@dataclass(frozen=True)
class NotionPerson:
    """선택 메뉴에 이름으로 보일 워크스페이스 사람 한 명이다."""

    id: str = field(repr=False)
    name: str = field(repr=False)


@dataclass(frozen=True)
class NotionPeople:
    """토큰 주인 ID(개인 토큰만 있음)와 고를 수 있는 사람 목록이다."""

    owner_id: str | None = field(default=None, repr=False)
    people: tuple[NotionPerson, ...] = field(default=(), repr=False)


class NotionDirectory(Protocol):
    """토큰으로 토큰 주인과 사람 목록을 읽는 외부 조회 포트다."""

    def people(self, token: CredentialSecret) -> NotionPeople:
        """사람 정보 권한이 없으면 빈 목록을 돌려준다."""
        ...


def people_payload(value: NotionPeople) -> dict[str, object]:
    """설정 화면 응답으로만 쓰고 로그·상태 응답에는 넣지 않는다."""
    return {
        "ownerId": value.owner_id,
        "people": [{"id": person.id, "name": person.name} for person in value.people],
    }


def parse_people(value: object) -> NotionPeople:
    """로컬 응답의 키·타입·개수를 검사한 뒤 타입을 좁힌다."""
    if not isinstance(value, dict) or set(value) != {"ownerId", "people"}:
        raise ValueError("사람 목록 응답을 확인하세요.")
    owner, people = value["ownerId"], value["people"]
    if owner is not None and (not isinstance(owner, str) or not owner):
        raise ValueError("사람 목록 응답을 확인하세요.")
    if not isinstance(people, list) or len(people) > MAX_PEOPLE:
        raise ValueError("사람 목록 응답을 확인하세요.")
    result: list[NotionPerson] = []
    for item in people:
        if not isinstance(item, dict) or set(item) != {"id", "name"}:
            raise ValueError("사람 목록 응답을 확인하세요.")
        identifier, name = item["id"], item["name"]
        if not isinstance(identifier, str) or not identifier or not isinstance(name, str):
            raise ValueError("사람 목록 응답을 확인하세요.")
        result.append(NotionPerson(identifier, name[:200]))
    return NotionPeople(owner, tuple(result))
