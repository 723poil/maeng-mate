"""수집과 발송에서 같은 규칙 범위와 관측 대상을 선택한다."""

from src.domain.models import CalendarEvent, SourceMapping, Task, WorkItem
from src.domain.source_alerts import SourceAlertRule
from src.domain.source_fingerprints import alert_scope_fingerprint, fingerprint, source_fingerprint
from src.domain.work_dates import canonical_identity


def field_alert_scopes(source: SourceMapping, user: str | None) -> dict[str, SourceAlertRule]:
    """규칙, 원본 매핑, 본인 범위를 한 발송 범위로 묶는다."""
    identity = canonical_identity(user) if user else None
    mapping = source_fingerprint(source)
    return {
        fingerprint(
            (
                source.source_id,
                mapping,
                identity if source.has_person_scope or rule.member == "self" else None,
                rule.id,
                rule.column_id,
                rule.comparison_kind,
                rule.target,
                *((rule.member, rule.item_filter) if rule.member or rule.item_filter else ()),
                *((rule.schedule.mode, rule.schedule.value) if rule.schedule else ()),
                *((digest,) if (digest := alert_scope_fingerprint(rule)) else ()),
            )
        ): rule
        for rule in source.alert_rules
        if rule.enabled
        and (
            user is not None
            or (rule.member != "self" and (rule.member is not None or not source.has_person_scope))
        )
    }


def includes_item(source: SourceMapping, item: WorkItem, user: str | None) -> bool:
    """기존 본인 범위를 따라 취소 항목은 알림 기준에서 제외한다."""
    if isinstance(item, CalendarEvent) and item.cancelled:
        return False
    if not source.has_person_scope:
        return True
    people = (
        item.assignee_ids
        if isinstance(item, Task)
        else item.participant_ids
        if isinstance(item, CalendarEvent)
        else item.person_ids
    )
    return user is not None and canonical_identity(user) in map(canonical_identity, people)
