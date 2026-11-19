"""업무 상태의 표시색을 결정하며 업무 완료 판정에는 관여하지 않는다."""

from src.domain.task_display import TaskDisplayGroup, display_group


def status_tone(status: str) -> str:
    """알려진 표현만 색으로 구별하고 새 상태는 중립색으로 표시한다."""
    if status == "반려":
        return "orange"
    if status == "버그아님":
        return "neutral"
    return {
        TaskDisplayGroup.REVIEW: "purple",
        TaskDisplayGroup.HOLD: "orange",
        TaskDisplayGroup.IN_PROGRESS: "blue",
        TaskDisplayGroup.COMPLETED: "green",
    }.get(display_group(status), "neutral")
