"""비밀값과 실행 경로를 제외한 일반·프로바이더 설정 값이다."""

from dataclasses import dataclass, field
from typing import Literal


@dataclass(frozen=True)
class GeneralSettings:
    """개인 업무 조회 정책과 예약이 없는 자료의 유효시간이다."""

    relevance_policy: Literal["assignment", "participation"] = "participation"
    snapshot_max_age_seconds: int = 1200


@dataclass(frozen=True)
class NotionSettings:
    """인증 토큰과 분리해 보관하는 Notion 본인 식별자다."""

    user_id: str | None = field(default=None, repr=False)


@dataclass(frozen=True)
class SlackSettings:
    """토큰과 분리해 저장하는 개인 DM 수신자 선택이다."""

    user_id: str | None = field(default=None, repr=False)


@dataclass(frozen=True)
class RuntimeSettings:
    """같은 버전으로 저장하고 적용하는 일반·프로바이더 설정이다."""

    revision: int = 0
    general: GeneralSettings = field(default_factory=GeneralSettings)
    notion: NotionSettings = field(default_factory=NotionSettings)
    slack: SlackSettings = field(default_factory=SlackSettings)
