"""회사별 일정 분류명은 코드 대신 설정으로 판단한다."""

from collections.abc import Mapping

from src.domain.models import EventType

EVENT_CLASSIFICATION = "eventClassification"


def classify_event(category: str | None, rules: Mapping[str, EventType]) -> EventType:
    """빈 값과 새 유형을 회의나 승인된 휴가로 추측하지 않는다."""
    return rules.get(category, "other") if category is not None else "other"
