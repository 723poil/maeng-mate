"""수집 방식별 초기 역할과 소스별 편집 정의를 관리한다."""

from dataclasses import dataclass
from uuid import UUID

from src.domain.display_columns import DISPLAY_COLUMN_TYPES, TITLE_COLUMN_TYPES
from src.domain.models import SourceKind


@dataclass(frozen=True)
class ColumnRole:
    """내부 필드가 받을 수 있는 원천 칼럼 타입이다."""

    key: str
    title: str
    types: tuple[str, ...]
    required: bool = True


TITLE = ColumnRole("title", "항목 제목", TITLE_COLUMN_TYPES)
STATUS = ("status", "select")
PERIOD = ColumnRole("period", "일정 시작·종료", ("date",))
ROLE_TEMPLATES: dict[SourceKind, tuple[ColumnRole, ...]] = {
    "task": (
        TITLE,
        ColumnRole("assignees", "내 항목만 보기", ("people",)),
        ColumnRole("status", "상태로 사용", STATUS),
        ColumnRole("dueDate", "마감일로 사용", ("date",), False),
    ),
    "meeting": (
        TITLE,
        ColumnRole("participants", "내 일정만 보기", ("people",)),
        PERIOD,
        ColumnRole("category", "일정 유형", STATUS, False),
        ColumnRole("cancelled", "취소 여부", ("checkbox",), False),
    ),
    "leave": (
        ColumnRole("people", "휴가 대상자", ("people",)),
        PERIOD,
        ColumnRole("approval", "승인 상태", STATUS),
    ),
}


def source_roles(kind: SourceKind, raw: object) -> tuple[ColumnRole, ...]:
    """이전 문서는 기본 역할로 읽고 명시한 역할은 수집 계약과 대조한다."""
    defaults = {role.key: role for role in ROLE_TEMPLATES[kind]}
    if raw is None:
        return tuple(defaults.values())
    if not isinstance(raw, list):
        raise ValueError
    roles = {}
    for item in raw:
        if not isinstance(item, dict) or set(item) != {"id", "name", "types"}:
            raise ValueError
        key, name, types = item["id"], item["name"], item["types"]
        if not isinstance(key, str) or not isinstance(name, str) or not 0 < len(name.strip()) <= 80:
            raise ValueError
        if (
            not isinstance(types, list)
            or not types
            or any(t not in DISPLAY_COLUMN_TYPES for t in types)
        ):
            raise ValueError
        template = defaults.get(key)
        # 업무 의미가 있는 역할은 허용 타입을 넓혀 필수 계산을 깨뜨릴 수 없다.
        if template is None:
            if str(UUID(key)) != key or len(types) != 1:
                raise ValueError
        elif tuple(types) != template.types:
            raise ValueError
        if key in roles or len(set(types)) != len(types):
            raise ValueError
        roles[key] = ColumnRole(
            key, name.strip(), tuple(types), template.required if template else False
        )
    if not defaults.keys() <= roles.keys():
        raise ValueError
    return tuple(roles.values())


def roles_payload(roles: tuple[ColumnRole, ...]) -> list[dict[str, object]]:
    """역할 이름과 연결 식별자를 독립적으로 저장한다."""
    return [{"id": role.key, "name": role.title, "types": list(role.types)} for role in roles]
