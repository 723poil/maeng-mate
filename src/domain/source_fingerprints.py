"""기존 스냅샷과 알림 기준을 식별하는 의미 해시를 계산한다."""

import hashlib
import json
from dataclasses import asdict

from src.domain.main_list import ListFilter, MainListRules
from src.domain.models import SourceMapping
from src.domain.source_alerts import SourceAlertRule


def fingerprint(value: object) -> str:
    """객체 키 순서와 무관하게 기존 저장 형식과 동일한 해시를 계산한다."""
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()
    ).hexdigest()


def source_fingerprint(source: SourceMapping) -> str:
    """새 분류 규칙이 없는 기존 소스는 이전 수집 해시를 그대로 사용한다."""
    payload: dict[str, object] = {
        "kind": source.kind,
        "source_id": source.source_id,
        "properties": dict(source.properties),
        "approved_values": source.approved_values,
    }
    # 기본 설정을 생략해야 이전 스냅샷과 같은 소스로 인식한다.
    if source.provider != "notion":
        payload["provider"] = source.provider
    if source.origin != source.source_id:
        payload["origin"] = source.origin
    if column := source.classification_column:
        # 단일 칼럼의 고유 값 매핑은 이전과 같은 의미이므로 정상 스냅샷을 재사용한다.
        values = dict(source.classification_values)
        for rule in source.classification_rules:
            condition = rule.conditions[0]
            if (
                len(rule.conditions) != 1
                or condition.property_id != column.property_id
                or condition.value in values
                or condition.kind not in (column.kind, "status_or_select")
            ):
                payload["classificationRules"] = [asdict(r) for r in source.classification_rules]
                break
            values[condition.value] = rule.result
        payload["classification"] = (
            column.value_key,
            column.kind,
            values,
            *((source.classification_default,) if source.classification_default else ()),
        )
    if source.event_types:
        payload["event_types"] = dict(source.event_types)
    if source.section != "tasks":
        payload["section"] = source.section
    if source.round_title_property is not None:
        payload["round_title_property"] = source.round_title_property
        # 기존 소스의 매핑 해시는 유지해 이전 스냅샷도 읽을 수 있다.
        if (source.parent_label, source.parent_order) != (True, "legacy"):
            payload["parentMetadata"] = (
                source.parent_label,
                source.parent_order,
                source.parent_number_property,
            )
    if source.flexible:
        payload["flexible"] = True
    if source.default_event_type != "meeting":
        payload["default_event_type"] = source.default_event_type
    return fingerprint(payload)


def condition_identity(rules: MainListRules) -> dict[str, object]:
    """기존 조건은 저장 형식을 유지하고 원본 ID가 있는 조건만 표시명과 분리한다."""

    def identity(condition: ListFilter) -> dict[str, str]:
        """원본 선택값의 이름 변경은 실행권과 비교 기준에 영향을 주지 않는다."""
        return {
            "field": condition.field,
            "operator": condition.operator,
            **(
                {"value": condition.value}
                if condition.option_id is None
                else {"option_id": condition.option_id}
            ),
        }

    return {
        "match": rules.match,
        "filters": [identity(condition) for condition in rules.filters],
        "sorts": [asdict(item) for item in rules.sorts],
        "exclusions": [identity(condition) for condition in rules.exclusions],
    }


def alert_scope_fingerprint(rule: SourceAlertRule) -> str | None:
    """비교식과 원본 연결이 같을 때만 같은 범위의 예약과 기준을 이어 쓴다."""
    if rule.source_scopes is not None and rule.item_filter is None:
        return fingerprint(
            (
                condition_identity(rule.scope) if rule.scope else None,
                rule.scope_bindings,
                [
                    {"source_id": sc.source_id, "conditions": condition_identity(sc.conditions)}
                    for sc in rule.source_scopes
                ],
            )
        )
    return (
        fingerprint((condition_identity(rule.scope), rule.scope_bindings)) if rule.scope else None
    )
