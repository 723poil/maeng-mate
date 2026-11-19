"""SQLite 설정 문서의 허용 항목·검증과 저장 계약이다."""

import re
from typing import Protocol

from src.domain.runtime_settings import (
    GeneralSettings,
    NotionSettings,
    RuntimeSettings,
    SlackSettings,
)


class RuntimeSettingsConflictError(Exception):
    """다른 화면에서 확정한 설정을 이전 버전으로 덮어쓰지 못하게 한다."""


class RuntimeSettingsStore(Protocol):
    """조회·최초 가져오기·버전 조건부 저장을 제공한다."""

    def read(self) -> RuntimeSettings | None:
        """미초기화 상태만 None으로 반환하고 읽기 오류는 전파한다."""
        ...

    def initialize(self, value: RuntimeSettings) -> RuntimeSettings:
        """최초 한 번만 저장하고 이미 설정되어 있으면 확정값을 유지한다."""
        ...

    def save(self, value: RuntimeSettings) -> RuntimeSettings:
        """읽은 버전이 같을 때만 저장하며 미초기화는 버전0에서 생성한다."""
        ...


def runtime_settings_payload(value: RuntimeSettings) -> dict[str, object]:
    """환경 전체 대신 허용된 일반·프로바이더 설정만 직렬화한다."""
    return {
        "revision": value.revision,
        "general": {
            "relevancePolicy": value.general.relevance_policy,
            "snapshotMaxAgeSeconds": value.general.snapshot_max_age_seconds,
        },
        "providers": {
            "notion": {"userId": value.notion.user_id},
            "slack": {"userId": value.slack.user_id},
        },
    }


def parse_runtime_settings(
    value: object, *, current: RuntimeSettings | None = None
) -> RuntimeSettings:
    """토큰·개인 경로·미지원 키와 암묵적인 타입 변환을 거부한다."""
    if not isinstance(value, dict) or set(value) != {"revision", "general", "providers"}:
        raise ValueError("일반·프로바이더 설정 형식을 확인하세요.")
    revision, general, providers = value["revision"], value["general"], value["providers"]
    if type(revision) is not int or revision < 0:
        raise ValueError("설정 버전을 확인하세요.")
    if not isinstance(general, dict) or set(general) != {
        "relevancePolicy",
        "snapshotMaxAgeSeconds",
    }:
        raise ValueError("일반 설정 항목을 확인하세요.")
    policy, age = general["relevancePolicy"], general["snapshotMaxAgeSeconds"]
    if policy not in ("assignment", "participation"):
        raise ValueError("업무 조회 정책을 확인하세요.")
    if type(age) is not int or not 1 <= age <= 86400:
        raise ValueError("자료 유효시간은 1~86400초로 지정하세요.")
    if (
        not isinstance(providers, dict)
        or "notion" not in providers
        or set(providers) - {"notion", "slack"}
    ):
        raise ValueError("지원하는 프로바이더 설정을 확인하세요.")
    notion = providers["notion"]
    if not isinstance(notion, dict) or set(notion) != {"userId"}:
        raise ValueError("Notion 설정 항목을 확인하세요.")
    user_id = notion["userId"]
    if user_id is not None and (not isinstance(user_id, str) or not user_id.strip()):
        raise ValueError("Notion 본인 ID를 확인하세요.")
    slack = current.slack if current else SlackSettings()
    if "slack" in providers:
        raw = providers["slack"]
        if not isinstance(raw, dict) or set(raw) != {"userId"}:
            raise ValueError("Slack 설정 항목을 확인하세요.")
        slack = SlackSettings(parse_slack_user(raw["userId"]))
    return RuntimeSettings(
        revision,
        GeneralSettings("assignment" if policy == "assignment" else "participation", age),
        NotionSettings(user_id),
        slack,
    )


def parse_slack_user(value: object) -> str | None:
    """빈 값은 미등록으로 처리하고 워크스페이스 ID와 임의 문자열을 거부한다."""
    if value is None or value == "":
        return None
    if not isinstance(value, str) or re.fullmatch(r"[UW][A-Z0-9]{8,63}", value) is None:
        raise ValueError("Slack 본인 멤버 ID를 확인하세요.")
    return value
