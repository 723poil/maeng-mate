"""수집 스냅샷의 저장 형식과 매핑·항목의 의미 해시를 관리한다."""

import json
from collections.abc import Iterator, Sequence

from src.application.errors import SnapshotError
from src.application.models import SyncRun
from src.domain.models import CalendarEvent, SourceMapping, Task, WorkItem
from src.domain.source_fingerprints import alert_scope_fingerprint, fingerprint, source_fingerprint


def collection_fingerprint(source: SourceMapping) -> str:
    """읽어야 할 표시 속성이 바뀌면 정기 수집만 앞당긴다."""
    digest = source_fingerprint(source)
    # 이전 문자열 미리보기는 원형 자료로 추정하지 않고 다음 정상 수집을 요청한다.
    if source.kind == "task" and source.storage_layout:
        digest = fingerprint((digest, "raw_fields_v1"))
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
    if item.raw_fields:
        payload["raw_fields"] = dict(item.raw_fields)
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
