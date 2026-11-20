"""수집 스냅샷의 저장 형식과 매핑·항목의 의미 해시를 관리한다."""

import hashlib
import json
from collections.abc import Iterator, Sequence
from dataclasses import asdict

from src.application.errors import SnapshotError
from src.application.models import SyncRun
from src.domain.main_list import ListFilter, MainListRules
from src.domain.models import CalendarEvent, SourceMapping, Task, WorkItem
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


def collection_fingerprint(source: SourceMapping) -> str:
    """읽어야 할 표시 속성이 바뀌면 정기 수집만 앞당긴다."""
    digest = source_fingerprint(source)
    columns = source.collected_columns()
    if not columns and not any(rule.enabled and rule.source_scopes for rule in source.alert_rules):
        return digest
    stored = [
        (item.value_key, item.property_id, "time")
        if item.time_only
        else (item.value_key, item.property_id)
        for item in source.storage_layout
        if item.origin is not None
    ]
    # 기존 미리보기만 저장했던 칼럼에 전체 값이 필요해지면 다음 수집을 앞당긴다.
    full_text = sorted(item.value_key for item in source.storage_layout if item.full_text)
    # 새 예약은 같은 칼럼을 이미 수집 중이어도 다음 정기 주기를 기다리면 시각을 놓칠 수 있다.
    schedules = sorted(
        (rule.id, rule.column_id, rule.comparison_kind, rule.schedule.mode, rule.schedule.value)
        for rule in source.alert_rules
        if rule.enabled and rule.schedule is not None
    )
    members = sorted(
        (rule.id, rule.column_id, rule.member)
        for rule in source.alert_rules
        if rule.enabled and rule.member is not None
    )
    scopes = sorted(
        (rule.id, alert_scope_fingerprint(rule))
        for rule in source.alert_rules
        if rule.enabled and (rule.scope or rule.source_scopes is not None)
    )
    if scopes:
        return fingerprint(
            (digest, sorted(columns.items()), sorted(stored), full_text, schedules, members, scopes)
        )
    if members:
        return fingerprint(
            (digest, sorted(columns.items()), sorted(stored), full_text, schedules, members)
        )
    if schedules:
        return fingerprint((digest, sorted(columns.items()), sorted(stored), full_text, schedules))
    if full_text:
        return fingerprint((digest, sorted(columns.items()), sorted(stored), full_text))
    return (
        fingerprint((digest, sorted(columns.items()), sorted(stored)))
        if stored
        else fingerprint((digest, sorted(columns.items())))
    )


def snapshot_payload(item: WorkItem) -> dict[str, object]:
    """재귀 복사 없이 저장 JSON의 필드와 기존 순서를 구성한다."""
    payload: dict[str, object] = {
        "id": item.id,
        "source_id": item.source_id,
        "url": item.url,
        "source_edited_at": item.source_edited_at,
        "observed_at": item.observed_at,
    }
    # 선택 칼럼이 없는 기존 소스는 빈 필드를 생략한 동일한 해시를 유지한다.
    if item.display_values:
        payload["display_values"] = dict(item.display_values)
    if item.alert_values:
        payload["alert_values"] = dict(item.alert_values)
    if item.calendar_periods:
        payload["calendar_periods"] = {
            key: period.payload() for key, period in item.calendar_periods.items()
        }
    if isinstance(item, Task):
        if item.parent_label is not None and item.qa_round is None:
            payload["parent_label"] = item.parent_label
        if item.parent_order is not None:
            payload["parent_order"] = item.parent_order
        payload.update(
            title=item.title,
            assignee_ids=item.assignee_ids,
            status=item.status,
            due=item.due.payload() if item.due else None,
            qa_round=item.qa_round,
        )
    elif isinstance(item, CalendarEvent):
        payload.update(
            title=item.title,
            participant_ids=item.participant_ids,
            period=item.period.payload() if item.period else None,
            category=item.category,
            event_type=item.event_type,
            cancelled=item.cancelled,
        )
    else:
        payload.update(
            person_ids=item.person_ids,
            period=item.period.payload(),
            approval=item.approval,
            approved=item.approved,
        )
    payload["kind"] = item.kind
    return payload


def staging_rows(run: SyncRun, items: Sequence[WorkItem]) -> Iterator[tuple[int, str, str, str]]:
    """스냅샷 원문과 업무 값 해시를 한 행씩 만들어 SQLite에 넘긴다."""
    # 칼럼 종류가 나중에 바뀌어도 이전 자유 텍스트를 규칙의 선택값으로 제안하지 않는다.
    option_fields = (
        tuple(
            column.value_key
            for column in run.source.storage_layout
            if column.origin is not None and column.kind in {"select", "status"}
        )
        if run.source
        else ()
    )
    for item in items:
        if item.source_id != run.source_id or item.kind != run.kind:
            raise SnapshotError("invalid_snapshot")
        payload = snapshot_payload(item)
        facts = {
            key: value
            for key, value in payload.items()
            if key not in {"observed_at", "source_edited_at"}
        }
        if option_fields:
            # 원본 값은 display_values에 한 번만 저장하고 수집 타입 근거는 변경 비교에서 제외한다.
            payload["option_fields"] = option_fields
        yield run.id, item.id, json.dumps(payload, ensure_ascii=False), fingerprint(facts)
