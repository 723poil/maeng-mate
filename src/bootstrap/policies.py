"""등록된 업무 전략을 시작 시 선택한다."""

from src.domain.policies import AssignmentPolicy, ParticipationPolicy, RelevancePolicy


def select_policy(name: str) -> RelevancePolicy:
    """등록된 전략만 조립하며 임의 모듈을 실행하지 않는다."""
    policies: dict[str, RelevancePolicy] = {
        "assignment": AssignmentPolicy(),
        "participation": ParticipationPolicy(),
    }
    if name not in policies:
        raise ValueError("지원하지 않는 관련성 전략")
    return policies[name]
