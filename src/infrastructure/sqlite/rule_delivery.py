"""카테고리 규칙의 문구와 채널을 저장된 설정에서 발송 직전까지 확인한다."""

import json
import sqlite3
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from src.application.notifications import NotificationJob
from src.application.notion_links import notion_link
from src.application.work_settings import read_category
from src.domain.category_event_alerts import EVENT_ALERT_TRIGGERS, CategoryEventAlert
from src.domain.source_alerts import SourceAlertRule
from src.domain.source_display import SourceCategory


@dataclass(frozen=True)
class RuleRoute:
    """채널에 보낼 제목·본문과 카테고리가 허용하는 규칙 채널이다."""

    title: str
    message: str | None
    channels: tuple[str, ...] | None


type DeliveryRules = Mapping[str, RuleRoute]


def category_rules(category: SourceCategory) -> dict[str, RuleRoute]:
    """사용자가 고른 채널은 카테고리가 허용한 범위에서만 활성화한다."""
    config = category.configuration
    if config is None:
        return {}

    category_channels = config.channels

    def route(rule: SourceAlertRule | CategoryEventAlert) -> RuleRoute:
        """이전 문서에 개별 설정이 없으면 원래 채널과 본문을 유지한다."""
        if rule.delivery is None:
            return RuleRoute(rule.name, None, None)
        channels = tuple(
            key
            for key in rule.delivery.channels
            if category_channels is None or key in category_channels
        )
        return RuleRoute(rule.name, rule.delivery.message, channels)

    rules = {
        entry.rule.notification_kind: route(entry.rule)
        for entry in config.alerts
        if entry.rule.enabled
    }
    # 이전 실행본이 만든 대기 건만 이어받고 새 수집은 UUID 규칙으로 발송한다.
    for entry in config.alerts:
        if entry.rule.enabled and entry.rule.previous_kind:
            rules[entry.rule.previous_kind] = route(entry.rule)
    for event in config.event_alerts or ():
        for kind in EVENT_ALERT_TRIGGERS[event.trigger].kinds:
            rules[kind] = route(event)
    return rules


def read_rules(
    connection: sqlite3.Connection, group: str | None, active: Mapping[str, DeliveryRules]
) -> DeliveryRules | None:
    """실행 중에도 저장한 문구·채널을 반영하고 삭제한 카테고리는 차단한다."""
    if group is None or not group.startswith("category:"):
        return None
    row = connection.execute(
        """SELECT (SELECT item.value FROM json_each(payload,'$.categories') AS item
        WHERE json_extract(item.value,'$.id')=?) AS category FROM work_settings WHERE id=1""",
        (group.removeprefix("category:"),),
    ).fetchone()
    if row is None:
        return active.get(group)
    if row["category"] is None:
        return {}
    category = read_category(json.loads(row["category"]))
    if category is None:
        raise ValueError("알림 카테고리를 확인하세요.")
    return category_rules(category)


def channel_kinds(
    kinds: Sequence[str], channel: str, rules: DeliveryRules | None
) -> tuple[str, ...]:
    """한 채널에서 실제로 허용된 규칙만 실행권에 연결한다."""
    if rules is None:
        return tuple(kinds)
    return tuple(
        kind
        for kind in kinds
        if (rule := rules.get(kind)) is not None
        and (rule.channels is None or channel in rule.channels)
    )


def channel_messages(rules: DeliveryRules, channel: str) -> dict[str, tuple[str, str | None]]:
    """설정 변경 전후에 이 채널에 보낼 수 있는 제목·본문만 비교한다."""
    return {
        kind: (rules[kind].title, rules[kind].message)
        for kind in channel_kinds(tuple(rules), channel, rules)
    }


def delivery_job(row: sqlite3.Row, token: str, rules: DeliveryRules | None) -> NotificationJob:
    """한 변경이 여러 규칙에 맞아도 해당 채널에 허용한 문구만 조합한다."""
    title, body = row["title"], row["body"]
    kinds: object = json.loads(row["kinds"])
    if not isinstance(kinds, list) or not all(isinstance(kind, str) for kind in kinds):
        raise ValueError("알림 종류를 확인하세요.")
    selected = [rules[kind] for kind in kinds if rules is not None and kind in rules]
    if selected:
        if any(rule.message is not None for rule in selected):
            title = " · ".join(dict.fromkeys(rule.title for rule in selected))
            body = "\n".join(
                dict.fromkeys(
                    rule.message if rule.message is not None else body for rule in selected
                )
            )
        else:
            title = selected[0].title
    return NotificationJob(
        row["id"], token, title, body, row["channel"], notion_link(row["notion_url"])
    )
