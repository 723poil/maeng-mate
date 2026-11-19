"""시작 경계에서만 Notion 비밀값을 해석하고 어댑터를 조립한다."""

import sys
from collections.abc import Mapping, Sequence

from src.application.credentials import CredentialError, CredentialSecret
from src.application.notion_credentials import NotionCredentialsService
from src.bootstrap.environment import ConfigError
from src.bootstrap.keychain import KeychainAccount
from src.domain.models import SourceMapping
from src.infrastructure.notion.connection import NotionConnection


def load_notion_environment(
    env: Mapping[str, str], *, keep_api_available: bool = False
) -> dict[str, str]:
    """보안 저장소를 우선하고 삭제·접근 오류에서 환경값을 되살리지 않는다."""
    values = dict(env)
    if sys.platform != "darwin":
        return values
    try:
        record = KeychainAccount.NOTION.store().read()
        if record is not None:
            values["NOTION_TOKEN"] = record.token.value if record.token else ""
    except CredentialError:
        if not keep_api_available:
            raise ConfigError("Keychain 접근을 확인하세요.") from None
        # 보안 오류는 수집을 중단하되 로컬 인증 설정 화면은 복구에 사용할 수 있게 한다.
        values["NOTION_TOKEN"] = ""
    return values


def build_notion_credentials(
    token: str | None, sources: Sequence[SourceMapping], *, legacy_token: str | None = None
) -> NotionCredentialsService | None:
    """UI에는 상태 포트만 전달하고 실제 비밀값은 서버에 남긴다."""
    if sys.platform != "darwin":
        return None
    active: CredentialSecret | None = CredentialSecret(token) if token else None
    return NotionCredentialsService(
        KeychainAccount.NOTION.store(),
        NotionConnection(sources),
        legacy=CredentialSecret(legacy_token) if legacy_token else None,
        active=active,
    )
