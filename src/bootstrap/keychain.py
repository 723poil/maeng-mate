"""앱이 사용하는 Keychain 항목과 계정별 쓰기 잠금을 조립한다."""

from enum import Enum
from typing import TYPE_CHECKING

from src.bootstrap.build_profile import current_profile
from src.bootstrap.instance import state_directory

if TYPE_CHECKING:
    from src.infrastructure.macos_keychain import MacKeychain


class KeychainAccount(Enum):
    """설정 편집·발송·설치가 공유하는 기존 Keychain 계정 목록이다."""

    LOCAL_API = ("local-api", "Local API")
    NOTION = ("notion", "Notion")
    SLACK_BOT = ("slack", "Slack")
    SLACK_APP = ("slack-app", "Slack App")

    def __init__(self, account: str, label: str) -> None:
        """저장 키와 사용자에게 표시할 이름을 구분한다."""
        self.account = account
        self.label = label

    def store(self) -> "MacKeychain":
        """macOS에서 해당 저장소가 필요할 때만 Security 프레임워크를 불러온다."""
        from src.infrastructure.macos_keychain import MacKeychain

        return MacKeychain(
            state_directory() / f"{self.account}-keychain.lock",
            service=f"{current_profile().identifier}.credentials",
            account=self.account,
            label=f"Maeng Mate · {self.label}",
        )
