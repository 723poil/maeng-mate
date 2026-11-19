"""근무 규칙 파일의 저장값과 이번 실행에 적용된 값을 구분한다."""

from dataclasses import dataclass
from typing import Protocol

from src.domain.daily_work import DEFAULT_WORK_RULES, WorkRules, parse_rules


class WorkRulesConflictError(Exception):
    """다른 저장으로 바뀐 파일을 이전 입력으로 덮지 않는다."""


@dataclass(frozen=True)
class WorkRulesDocument:
    """버전과 설정값이 하나의 JSON으로 저장된다."""

    revision: int
    rules: WorkRules


@dataclass(frozen=True)
class WorkRulesState:
    """편집할 값과 다시 시작할 필요가 있는지를 표시한다."""

    settings: WorkRulesDocument
    restart_required: bool

    @property
    def initialized(self) -> bool:
        """파일이 있을 때만 저장된 설정으로 표시한다."""
        return self.settings.revision > 0


def document_payload(value: WorkRulesDocument) -> dict[str, object]:
    """지원하는 파일 형식으로 작성한다."""
    return {"version": 1, "revision": value.revision, "rules": value.rules.payload()}


def parse_document(value: object) -> WorkRulesDocument:
    """임의 필드와 잘못된 버전을 허용하지 않는다."""
    if (
        not isinstance(value, dict)
        or set(value) != {"version", "revision", "rules"}
        or type(value["version"]) is not int
        or value["version"] != 1
        or type(value["revision"]) is not int
        or value["revision"] < 0
    ):
        raise ValueError("근무 규칙의 파일 형식을 확인하세요.")
    rules = parse_rules(value["rules"])
    if rules is None:
        raise ValueError("근무 규칙이 비어 있습니다.")
    return WorkRulesDocument(value["revision"], rules)


class WorkRulesStore(Protocol):
    """파일과 잠금 구현을 설정 유스케이스에서 분리한다."""

    def read(self) -> WorkRulesDocument | None:
        """파일이 없을 때만 None을 반환한다."""
        ...

    def save(self, document: WorkRulesDocument) -> WorkRulesDocument:
        """현재 버전일 때 설정을 교체한다."""
        ...


class WorkRulesClient(Protocol):
    """관리자 설정 화면이 사용하는 두 동작이다."""

    def read(self) -> WorkRulesState:
        """저장값과 적용 상태를 조회한다."""
        ...

    def save(self, value: WorkRulesDocument) -> WorkRulesState:
        """현재 버전의 설정을 저장한다."""
        ...


class WorkRulesService:
    """첫 편집은 기존 설정을 이어받고 적용은 다음 앱 실행에서 한다."""

    def __init__(self, store: WorkRulesStore, legacy: WorkRules | None) -> None:
        """설치된 규칙과 이전 DB 설정으로 이번 실행의 규칙을 고정한다."""
        self._store = store
        self._initial = WorkRulesDocument(0, legacy if legacy is not None else DEFAULT_WORK_RULES)
        self.active = (store.read() or self._initial).rules

    def read(self) -> WorkRulesState:
        """파일이 없으면 최초 편집값만 반환한다."""
        value = self._store.read() or self._initial
        return WorkRulesState(value, value.rules != self.active)

    def save(self, value: WorkRulesDocument) -> WorkRulesState:
        """명시적으로 저장한 규칙만 설정 파일을 갱신한다."""
        saved = self._store.save(value)
        return WorkRulesState(saved, saved.rules != self.active)
