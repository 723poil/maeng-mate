"""원본 표와 속성 ID에 직접 연결한 알림 조건이다."""

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from src.domain.main_list import ListFilter, MainListRules

if TYPE_CHECKING:
    from src.domain.models import SourceMapping


def field_parts(field: str) -> tuple[str, str]:
    """같은 이름이나 같은 속성 ID를 서로 다른 표에서 혼동하지 않게 종류와 ID를 읽는다."""
    kind, sep, identifier = field.partition(":")
    if not sep or not identifier or kind not in ("text", "period", "boolean", "number"):
        raise ValueError("원본 칼럼 연결을 확인하세요.")
    return kind, identifier


@dataclass(frozen=True)
class SourceScope:
    """한 표에서만 유효한 원본 칼럼 조건이다. 빈 조건은 해당 표 전체를 뜻한다."""

    source_id: str
    conditions: MainListRules

    def validate(self) -> None:
        """원본 속성에 맞는 연산자와 값만 저장한다."""
        self.conditions.validate({key: field_parts(key)[0] for key in self.conditions.fields})


def calendar_conditions(
    source: "SourceMapping", kind: Literal["meeting", "upcoming"]
) -> MainListRules | None:
    """소스에 연결된 실제 분류 선택값과 기간 속성에서 종전의 대상 조건을 복원한다."""
    if source.kind != "meeting":
        return None
    if kind == "upcoming":
        return MainListRules(
            filters=(ListFilter("period:" + source.properties["period"], "not_ended"),)
        )
    prop = source.properties.get("category")
    if prop is None:
        return MainListRules() if source.default_event_type == "meeting" else None
    choices = tuple(
        ListFilter("text:" + prop, "exact", name)
        for name, value in source.event_types.items()
        if value == "meeting"
    )
    return MainListRules("any" if len(choices) > 1 else "all", choices) if choices else None
