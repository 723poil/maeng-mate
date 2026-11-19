"""프레임워크와 비밀 설정 없이 시스템 상태를 구성한다."""

from src.application.models import SystemStatusContext


def get_system_status(context: SystemStatusContext) -> dict[str, object]:
    """설정 존재 여부와 현재 구현 범위만 반환한다."""
    return {
        "phase": "personal_queries",
        "runtime": "python",
        "automaticExecution": "disabled",
        "configuration": {
            "database": context.database_configured,
            "notion": context.notion_configured,
            "sources": context.sources_configured,
            "retention": context.retention_confirmed,
        },
        "workRelevancePolicy": context.relevance_policy,
        "unimplemented": ["slack", "ai_summary"],
    }
