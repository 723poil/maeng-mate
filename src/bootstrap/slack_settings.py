"""Slack 설정의 SQLite·Keychain·인증 확인 구현을 조립한다."""

import sys

from src.application.slack_notifications import SlackDeliveryStore, SlackNotificationDelivery
from src.application.slack_settings import SlackSettingsService
from src.bootstrap.environment import AppEnvironment
from src.bootstrap.keychain import KeychainAccount
from src.domain.runtime_settings import GeneralSettings, NotionSettings, RuntimeSettings
from src.infrastructure.slack_auth import SlackAuth
from src.infrastructure.slack_messages import SlackMessages
from src.infrastructure.sqlite.connection import Database
from src.infrastructure.sqlite.runtime_settings import SQLiteRuntimeSettings


def build_slack_settings(environment: AppEnvironment) -> SlackSettingsService | None:
    """설정·인증과 명시적 테스트 발송에 필요한 포트를 연결한다."""
    if sys.platform != "darwin" or environment.database is None:
        return None
    active = RuntimeSettings(
        general=GeneralSettings(
            "assignment" if environment.relevance_policy == "assignment" else "participation",
            environment.snapshot_max_age_seconds,
        ),
        notion=NotionSettings(environment.notion_user_id),
    )
    return SlackSettingsService(
        SQLiteRuntimeSettings(Database(environment.database)),
        active,
        KeychainAccount.SLACK_BOT.store(),
        SlackAuth(),
        KeychainAccount.SLACK_APP.store(),
        SlackMessages(),
    )


def build_slack_delivery(
    environment: AppEnvironment, store: SlackDeliveryStore | None
) -> SlackNotificationDelivery | None:
    """기존 실행권 저장소와 서버 전용 보안 저장·전송을 연결한다."""
    if sys.platform != "darwin" or environment.database is None or store is None:
        return None
    return SlackNotificationDelivery(
        store,
        SQLiteRuntimeSettings(Database(environment.database)),
        KeychainAccount.SLACK_BOT.store(),
        SlackMessages(),
    )
