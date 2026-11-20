"""설정 저장 포트와 경계 검증·직렬화 계약이다."""

import re
from typing import Protocol

from src.application.notification_identity import identity_payload, parse_identity
from src.domain.calendar import CALENDAR_KINDS, CalendarKind, CalendarPreferences
from src.domain.notification_identity import NotificationIdentity
from src.domain.notification_timing import departure_minutes
from src.domain.preferences import (
    LEGACY_SECTIONS,
    NOTIFICATION_TYPES,
    SECTIONS,
    Preferences,
    SectionPreference,
)
from src.domain.source_alerts import is_source_alert_kind


class PreferencesConflictError(Exception):
    """이전 화면이 다른 설정 변경을 덮어쓰지 못하게 한다."""


class PreferencesStore(Protocol):
    """설정 조회와 버전 조건부 저장을 제공한다."""

    def read(self) -> Preferences:
        """확정 설정을 읽으며 미기록일 때만 기본값을 사용한다."""
        ...

    def save(self, value: Preferences) -> Preferences:
        """동일 버전일 때만 새 설정을 원자적으로 확정한다."""
        ...


class PreferencesClient(Protocol):
    """화면에 저장 구현 대신 설정 조회·수정 계약을 주입한다."""

    def read(self) -> Preferences:
        """서버의 확정 설정을 조회한다."""
        ...

    def save(self, value: Preferences) -> Preferences:
        """사용자가 읽은 버전으로 설정을 수정한다."""
        ...


def preferences_payload(value: Preferences) -> dict[str, object]:
    """개인 식별자 없이 설정 선택만 직렬화한다."""
    return {
        "revision": value.revision,
        "sections": [
            {
                "key": item.key,
                "visible": item.visible,
            }
            for item in value.sections
        ],
        "notificationsEnabled": value.notifications_enabled,
        "channels": list(value.channels),
        "notificationTypes": list(value.notification_types),
        "notificationIdentity": identity_payload(value.notification_identity),
        "calendar": {
            "visibleKinds": list(value.calendar.visible_kinds),
            "hiddenCategories": list(value.calendar.hidden_categories),
            "shownCategories": list(value.calendar.shown_categories),
        },
        "mutedSourceNotifications": {
            key: list(events) for key, events in value.muted_source_notifications.items()
        },
    }


def selected_values(
    value: object, allowed: tuple[str, ...], *, departure: bool = False
) -> tuple[str, ...]:
    """중복과 미지원 선택을 거부하고 문자열만 반환한다."""
    if not isinstance(value, list):
        raise ValueError("설정 목록을 확인하세요.")
    result: list[str] = []
    for item in value:
        if (
            not isinstance(item, str)
            or item in result
            or (item not in allowed and not (departure and departure_minutes(item) is not None))
        ):
            raise ValueError("지원하는 설정을 선택하세요.")
        result.append(item)
    return tuple(result)


def parse_preferences(
    value: object, channels: tuple[str, ...] = ("macos",), *, current: Preferences | None = None
) -> Preferences:
    """외부 JSON의 키·타입·영역 순열을 모두 검증한다."""
    required = {
        "revision",
        "sections",
        "notificationsEnabled",
        "channels",
        "notificationTypes",
    }
    if (
        not isinstance(value, dict)
        or not required <= set(value)
        or set(value) - required - {"notificationIdentity", "calendar", "mutedSourceNotifications"}
    ):
        raise ValueError("설정 형식을 확인하세요.")
    revision, enabled, sections = (
        value["revision"],
        value["notificationsEnabled"],
        value["sections"],
    )
    if (
        type(revision) is not int
        or revision < 0
        or type(enabled) is not bool
        or not isinstance(sections, list)
    ):
        raise ValueError("설정 값을 확인하세요.")
    parsed: list[SectionPreference] = []
    for item in sections:
        if not isinstance(item, dict) or set(item) != {"key", "visible"}:
            raise ValueError("화면 설정을 확인하세요.")
        key, visible = item["key"], item["visible"]
        if (
            not isinstance(key, str)
            or (
                key not in (*SECTIONS, *LEGACY_SECTIONS)
                and re.fullmatch(r"(?:source|category):[A-Za-z0-9-]{1,64}", key) is None
            )
            or type(visible) is not bool
        ):
            raise ValueError("화면 선택을 확인하세요.")
        parsed.append(SectionPreference(key, visible))
    keys = {item.key for item in parsed}
    if len(keys) != len(parsed):
        raise ValueError("영역 순서가 중복되었습니다.")
    if keys.intersection(SECTIONS) == set(SECTIONS) - {"calendar"}:
        # 기존 문서의 선택·순서는 보존하고 새 영역만 추가한다.
        previous = (
            next(
                (item for item in current.sections if item.key == "calendar"),
                SectionPreference("calendar", False),
            )
            if current
            else SectionPreference("calendar", False)
        )
        position = (
            next(
                (index for index, item in enumerate(current.sections) if item.key == "calendar"),
                len(parsed),
            )
            if current
            else len(parsed)
        )
        parsed.insert(min(position, len(parsed)), previous)
    if len(parsed) > 128 or not set(SECTIONS) <= {item.key for item in parsed}:
        raise ValueError("영역 순서가 중복되었습니다.")
    calendar = current.calendar if current else CalendarPreferences()
    if "calendar" in value:
        calendar = parse_calendar_preferences(value["calendar"])
    muted = current.muted_source_notifications if current else {}
    if "mutedSourceNotifications" in value:
        muted = parse_muted_sources(value["mutedSourceNotifications"])
    return Preferences(
        revision,
        tuple(parsed),
        enabled,
        selected_values(value["channels"], channels),
        selected_values(value["notificationTypes"], NOTIFICATION_TYPES, departure=True),
        parse_identity(value["notificationIdentity"])
        if "notificationIdentity" in value
        else current.notification_identity
        if current
        else NotificationIdentity(),
        calendar,
        muted,
    )


def parse_muted_sources(value: object) -> dict[str, tuple[str, ...]]:
    """카테고리마다 지원하는 수집 알림 선택만 저장한다."""
    if not isinstance(value, dict) or len(value) > 128:
        raise ValueError("카테고리 알림 설정을 확인하세요.")
    result = {}
    for key, muted in value.items():
        if not isinstance(key, str) or (
            key not in {"tasks", "qa", "meetings"}
            and re.fullmatch(r"(?:source|category):[A-Za-z0-9-]{1,64}", key) is None
        ):
            raise ValueError("카테고리 알림 설정을 확인하세요.")
        if (
            not isinstance(muted, list)
            or any(
                not isinstance(kind, str)
                or (kind not in NOTIFICATION_TYPES and not is_source_alert_kind(kind))
                for kind in muted
            )
            or len(set(muted)) != len(muted)
        ):
            raise ValueError("카테고리 알림 설정을 확인하세요.")
        result[key] = tuple(muted)
    return result


def parse_calendar_preferences(value: object) -> CalendarPreferences:
    """달력 종류는 중복·미지원 선택을 거부하며 모두 숨김도 허용한다."""
    if (
        not isinstance(value, dict)
        or set(value) - {"visibleKinds", "hiddenCategories", "shownCategories"}
        or "visibleKinds" not in value
        or not isinstance(value["visibleKinds"], list)
    ):
        raise ValueError("캘린더 표시 설정을 확인하세요.")
    result: list[CalendarKind] = []
    for kind in value["visibleKinds"]:
        matched = next((candidate for candidate in CALENDAR_KINDS if candidate == kind), None)
        if matched is None or matched in result:
            raise ValueError("캘린더 표시 항목을 확인하세요.")
        result.append(matched)
    hidden = parse_calendar_categories(value.get("hiddenCategories", []))
    shown = parse_calendar_categories(value.get("shownCategories", []))
    if set(hidden).intersection(shown):
        raise ValueError("캘린더 카테고리를 확인하세요.")
    return CalendarPreferences(tuple(result), hidden, shown)


def parse_calendar_categories(value: object) -> tuple[str, ...]:
    """사용자 카테고리의 식별자와 중복을 검사한다."""
    if (
        not isinstance(value, list)
        or any(
            not isinstance(key, str) or re.fullmatch(r"category:[a-f0-9-]{36}", key) is None
            for key in value
        )
        or len(set(value)) != len(value)
    ):
        raise ValueError("캘린더 카테고리를 확인하세요.")
    return tuple(value)
