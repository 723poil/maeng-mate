"""개인 업무 조회의 입력과 저장소 읽기 결과다."""

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime

from src.domain.display_columns import DisplayColumn
from src.domain.main_list import MainListRules
from src.domain.models import SourceMapping, WorkItem
from src.domain.source_display import SourceCategory


@dataclass(frozen=True)
class MainListCard:
    """연결된 소스들이 공유하는 메인 카드와 규칙의 칼럼 타입이다."""

    key: str
    rules: MainListRules
    kinds: Mapping[str, str]


@dataclass(frozen=True)
class TodayContext:
    """조회 대상은 실행 설정으로 고정하고 사용자 입력으로 바꾸지 않는다."""

    user_id: str = field(repr=False)
    sources: tuple[SourceMapping, ...]
    completed_statuses: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    max_age_seconds: int = 1200
    source_pages: Mapping[str, str] = field(default_factory=dict)
    page_links_revision: int = 0
    desktop_areas: Mapping[str, str] = field(default_factory=dict)
    desktop_sort_directions: Mapping[str, str] = field(default_factory=dict, kw_only=True)
    source_categories: Mapping[str, SourceCategory] = field(default_factory=dict, kw_only=True)
    source_names: Mapping[str, str] = field(default_factory=dict, kw_only=True)
    main_cards: Mapping[str, MainListCard] = field(default_factory=dict, kw_only=True)
    display_columns: Mapping[str, tuple[DisplayColumn, ...]] = field(default_factory=dict)

    def category_fields(self, source: SourceMapping) -> dict[str, str]:
        """현재 설정의 카테고리 이름과 검색용 고정 ID를 함께 제공한다."""
        category = self.source_categories.get(source.source_id)
        return {"categoryId": category.id, "categoryName": category.name} if category else {}


@dataclass(frozen=True)
class SourceSnapshot:
    """정상 자료와 직전 실행 상태를 함께 전달하는 읽기 모델이다."""

    source_id: str
    kind: str
    last_success_at: datetime | None
    latest_run_status: str | None
    mapping_matches: bool
    items: tuple[WorkItem, ...]
    next_scheduled_at: datetime | None = None
    status_options: tuple[str, ...] | None = None
