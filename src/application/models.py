"""유스케이스의 실행 식별자와 안전한 입력 값이다."""

from dataclasses import dataclass, field

from src.domain.models import SourceMapping, WorkItem


@dataclass(frozen=True)
class CollectedWork:
    """한 번의 완전 수집에서 얻은 행과 원본 상태 칼럼의 선택값이다."""

    items: list[WorkItem]
    status_options: tuple[str, ...] | None = None


@dataclass(frozen=True)
class SyncRun:
    """소스의 실행 세대와 매핑 근거를 식별한다."""

    id: int
    source_id: str
    kind: str
    generation: int
    mapping_hash: str
    source: SourceMapping | None = field(default=None, repr=False, compare=False)


@dataclass(frozen=True)
class SystemStatusContext:
    """상태 조회에는 비밀값 대신 설정 여부만 전달한다."""

    database_configured: bool
    notion_configured: bool
    sources_configured: bool
    retention_confirmed: bool
    relevance_policy: str
