"""현재 수집 소스와 저장된 화면 순서로 표시 영역을 구성한다."""

from src.domain.preferences import SectionPreference
from src.domain.source_display import SourceDisplay, display_groups


def group_board_url(group: tuple[SourceDisplay, ...]) -> str | None:
    """묶음에 원본 주소가 하나일 때만 카드 전체의 이동 링크를 표시한다."""
    urls = tuple(dict.fromkeys(item.url for item in group if item.url))
    return urls[0] if len(urls) == 1 else None


def source_sections(
    saved: tuple[SectionPreference, ...], display: tuple[SourceDisplay, ...]
) -> tuple[SectionPreference, ...]:
    """기본 기능과 사용자가 연결한 카테고리만 표시한다."""
    active = tuple(display_groups(display))
    available = {"work", "calendar", *active}
    result = tuple(item for item in saved if item.key in available)
    present = {item.key for item in result}
    return (*result, *(SectionPreference(key) for key in active if key not in present))
