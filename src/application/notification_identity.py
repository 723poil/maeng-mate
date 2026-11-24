"""알림 발신 앱 설정의 검증과 네이티브 처리 경계다."""

import base64
import binascii
import struct
from typing import Protocol

from src.domain.notification_identity import NotificationIdentity

MAX_ICON_BYTES = 196608


def validate_identity(value: NotificationIdentity) -> NotificationIdentity:
    """이름의 제어 문자와 과도한 크기·비PNG 아이콘을 저장 전에 거부한다."""
    name = value.name.strip()
    if not 1 <= len(name) <= 40 or any(ord(char) < 32 or ord(char) == 127 for char in name):
        raise ValueError("앱 이름은 1~40자로 입력하세요.")
    icon = value.icon_png
    if icon is not None:
        if (
            len(icon) < 33
            or len(icon) > MAX_ICON_BYTES
            or icon[:8] != b"\x89PNG\r\n\x1a\n"
            or icon[12:16] != b"IHDR"
            or struct.unpack(">II", icon[16:24]) != (256, 256)
        ):
            raise ValueError("아이콘 이미지를 다시 선택하세요.")
    return NotificationIdentity(name, icon)


def identity_payload(value: NotificationIdentity) -> dict[str, object]:
    """로컬 API와 SQLite에서 같은 JSON 표현을 사용한다."""
    return {
        "name": value.name,
        "iconPng": base64.b64encode(value.icon_png).decode("ascii") if value.icon_png else None,
    }


def parse_identity(value: object) -> NotificationIdentity:
    """파일 경로나 실행 코드를 받지 않고 이름·PNG 값만 허용한다."""
    if not isinstance(value, dict) or set(value) != {"name", "iconPng"}:
        raise ValueError("알림 앱 설정을 확인하세요.")
    name, raw = value["name"], value["iconPng"]
    if not isinstance(name, str) or (raw is not None and not isinstance(raw, str)):
        raise ValueError("알림 앱 설정을 확인하세요.")
    try:
        icon = base64.b64decode(raw, validate=True) if isinstance(raw, str) else None
    except (ValueError, binascii.Error):
        raise ValueError("아이콘 이미지를 다시 선택하세요.") from None
    return validate_identity(NotificationIdentity(name, icon))


class NotificationBranding(Protocol):
    """설정 화면과 파일·서명·OS 등록 구현 사이의 경계다."""

    def apply(self, identity: NotificationIdentity, revision: int = 0) -> None:
        """같은 앱 식별자를 유지하며 이름·아이콘을 반영한다."""
        ...

    def test(self) -> str:
        """명시적 버튼 요청에서만 권한과 테스트 알림을 요청한다."""
        ...
