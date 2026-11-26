"""소스 알림의 예약·조건·전달 설정을 읽고 저장 형식으로 변환한다."""

from collections.abc import Mapping
from uuid import UUID

from src.application.list_settings_codec import filter_conditions_payload, parse_list_filters
from src.application.settings_validation import nonblank, optional_strings
from src.domain.category_configuration import CategoryColumn
from src.domain.category_event_alerts import EVENT_ALERT_KINDS
from src.domain.display_columns import DISPLAY_COLUMN_TYPES
from src.domain.event_classification import EVENT_CLASSIFICATION
from src.domain.main_list import MainListRules
from src.domain.source_alerts import (
    COLLECTION_STATE,
    MAX_SOURCE_ALERT_RULES,
    AlertDelivery,
    AlertSchedule,
    SourceAlertRule,
)
from src.domain.source_display import SOURCE_CHANNELS
from src.domain.source_scope import SourceScope
from src.domain.work_dates import canonical_identity


def parse_alert_delivery(value: object) -> AlertDelivery:
    """명시적인 빈 채널을 보존하고 알림 발신에 사용할 값만 허용한다."""
    if not isinstance(value, dict) or set(value) - {"bodyToken"} != {"message", "channels"}:
        raise ValueError
    channels = category_channels(value["channels"])
    message = value["message"]
    body_token = value.get("bodyToken")
    if body_token is not None and not isinstance(body_token, str):
        raise ValueError
    if message is not None and not isinstance(message, str):
        raise ValueError
    return AlertDelivery(message.strip() if message is not None else None, channels, body_token)


def alert_delivery_payload(delivery: AlertDelivery | None) -> dict[str, object]:
    """기존 문서는 전달 설정을 생략해 원래 본문과 채널 선택을 유지한다."""
    if delivery is None:
        return {}
    channels = list(delivery.channels) if delivery.channels is not None else None
    return {
        "delivery": {
            "message": delivery.message,
            "channels": channels,
            **({"bodyToken": delivery.body_token} if delivery.body_token is not None else {}),
        }
    }


def parse_alert_schedule(raw: object, kind: str, target: object) -> AlertSchedule:
    """날짜·시간 칼럼만 예약 대상으로 받고 값 변경 조건과 혼용하지 않는다."""
    if kind not in {"date", "time", "created_time", "last_edited_time"} or target is not None:
        raise ValueError("예약 규칙에는 날짜 또는 시간 칼럼을 선택하세요.")
    if not isinstance(raw, dict) or set(raw) != {"mode", "value"}:
        raise ValueError("알림 예약 조건을 확인하세요.")
    mode, value = raw["mode"], raw["value"]
    if mode not in ("same_day", "previous_day", "before_start") or not isinstance(value, str):
        raise ValueError("알림 예약 입력을 확인하세요.")
    return AlertSchedule(mode, value)


def parse_alert_scope(
    raw: object, columns: Mapping[str, CategoryColumn] | None
) -> MainListRules | None:
    """항목 조건은 카테고리의 저장 칼럼만 사용하며 빈 조건은 기본 범위로 취급한다."""
    if (
        columns is None
        or not isinstance(raw, dict)
        or set(raw) - {"exclusions"} != {"match", "filters"}
    ):
        raise ValueError
    match = raw["match"]
    if not isinstance(match, str):
        raise ValueError
    rules = MainListRules(
        match,
        parse_list_filters(raw["filters"]),
        exclusions=parse_list_filters(raw.get("exclusions", [])),
    )
    rules.validate(
        {EVENT_CLASSIFICATION: "text", **{key: col.kind for key, col in columns.items()}}
    )
    return rules if rules.fields else None


def parse_source_scopes(raw: object) -> tuple[SourceScope, ...]:
    """표별 조건과 원본 칼럼 참조를 검증하고 표 중복을 막는다."""
    if not isinstance(raw, list):
        raise ValueError
    result = []
    for item in raw:
        if not isinstance(item, dict) or set(item) - {"exclusions"} != {
            "sourceId",
            "match",
            "filters",
        }:
            raise ValueError
        rules = MainListRules(
            nonblank(item["match"]),
            parse_list_filters(item["filters"], source_options=True),
            exclusions=parse_list_filters(item.get("exclusions", []), source_options=True),
        )
        scope = SourceScope(canonical_identity(nonblank(item["sourceId"])), rules)
        scope.validate()
        result.append(scope)
    if len({item.source_id for item in result}) != len(result):
        raise ValueError
    return tuple(result)


def parse_alert_rules(
    raw: object,
    kinds: tuple[str, ...] = DISPLAY_COLUMN_TYPES,
    *,
    scope_columns: Mapping[str, CategoryColumn] | None = None,
) -> tuple[SourceAlertRule, ...]:
    """원본 속성과 규칙의 이름·조건·사용 여부를 확인한다."""
    if not isinstance(raw, list) or len(raw) > MAX_SOURCE_ALERT_RULES:
        raise ValueError
    rules = []
    for item in raw:
        if not isinstance(item, dict) or set(item) - {
            "delivery",
            "schedule",
            "member",
            "itemFilter",
            "sourceScopes",
            "scope",
            "previousKind",
        } != {
            "id",
            "name",
            "columnId",
            "columnKind",
            "target",
            "enabled",
        }:
            raise ValueError
        name, column, kind = map(nonblank, (item["name"], item["columnId"], item["columnKind"]))
        target, enabled = item["target"], item["enabled"]
        if (
            len(name) > 80
            or any(ord(char) < 32 for char in name)
            or kind not in kinds
            or (target is not None and (not isinstance(target, str) or len(target) > 240))
            or type(enabled) is not bool
        ):
            raise ValueError
        prior = item.get("previousKind")
        if prior is not None and (not isinstance(prior, str) or prior not in EVENT_ALERT_KINDS):
            raise ValueError
        member = item.get("member")
        if member is not None:
            if (
                not isinstance(member, str)
                or kind not in {"text", "people"}
                or target is not None
                or "schedule" in item
            ):
                raise ValueError
            if member != "self":
                member = str(UUID(member))
        item_filter = item.get("itemFilter")
        if item_filter not in (None, "meeting", "upcoming"):
            raise ValueError
        if "sourceScopes" in item and (
            scope_columns is None or item_filter is not None or column == COLLECTION_STATE
        ):
            raise ValueError
        value = None if target is None else " ".join(target.split())
        rules.append(
            SourceAlertRule(
                str(UUID(nonblank(item["id"]))),
                name,
                column,
                kind,
                value,
                enabled,
                delivery=parse_alert_delivery(item["delivery"]) if "delivery" in item else None,
                member=member,
                item_filter=item_filter,
                source_scopes=parse_source_scopes(item["sourceScopes"])
                if "sourceScopes" in item
                else None,
                scope=parse_alert_scope(item["scope"], scope_columns) if "scope" in item else None,
                previous_kind=prior,
                schedule=parse_alert_schedule(item["schedule"], kind, target)
                if "schedule" in item
                else None,
            )
        )
    if len({rule.id for rule in rules}) != len(rules):
        raise ValueError
    return tuple(rules)


def alert_rules_payload(rules: tuple[SourceAlertRule, ...]) -> list[dict[str, object]]:
    """설정 화면의 이름과 조건을 JSON으로 저장한다."""
    return [
        {
            "id": rule.id,
            "name": rule.name,
            "columnId": rule.column_id,
            "columnKind": rule.column_kind,
            "target": rule.target,
            "enabled": rule.enabled,
            **alert_delivery_payload(rule.delivery),
            **({"previousKind": rule.previous_kind} if rule.previous_kind else {}),
            **({"member": rule.member} if rule.member is not None else {}),
            **({"scope": filter_conditions_payload(rule.scope)} if rule.scope else {}),
            **(
                {
                    "sourceScopes": [
                        {"sourceId": sc.source_id, **filter_conditions_payload(sc.conditions)}
                        for sc in rule.source_scopes
                    ]
                }
                if rule.source_scopes is not None
                else {}
            ),
            **({"itemFilter": rule.item_filter} if rule.item_filter is not None else {}),
            **(
                {"schedule": {"mode": rule.schedule.mode, "value": rule.schedule.value}}
                if rule.schedule
                else {}
            ),
        }
        for rule in rules
    ]


def category_channels(raw: object) -> tuple[str, ...] | None:
    """카테고리에서 실제 사용할 수 있는 전달 채널을 검증한다."""
    result = optional_strings(raw)
    if result is not None and set(result) - set(SOURCE_CHANNELS):
        raise ValueError
    return result
