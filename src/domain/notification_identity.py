"""알림 발신 앱의 표시 이름과 아이콘 선택이다."""

from dataclasses import dataclass, field


@dataclass(frozen=True)
class NotificationIdentity:
    """시스템 식별자와 분리해 변경하는 사용자 표시 값이다."""

    name: str = "Maeng Mate"
    icon_png: bytes | None = field(default=None, repr=False)
