"""알림 키를 원천 식별자와 분리한다."""

import hashlib

from src.domain.work_dates import canonical_identity


def notification_key(value: str) -> str:
    """큐 식별자에 원천·사용자 ID를 그대로 드러내지 않는다."""
    return hashlib.sha256(value.encode()).hexdigest()


def departure_key(day: str, minutes: int) -> str:
    """같은 날짜와 간격의 퇴근 예약과 구독 판정에 같은 키를 사용한다."""
    return notification_key(f"departure:{day}:{minutes}")


def task_scope(source_id: str, user_key: str, mapping_hash: str) -> str:
    """업무 알림은 현재 본인과 현재 수집 매핑에만 연결한다."""
    return notification_key(f"task:{source_id}:{user_key}:{mapping_hash}")


def meeting_scope(source_id: str, user_id: str, mapping_hash: str) -> str:
    """회의 알림은 본인과 현재 소스 매핑 범위로 제한한다."""
    return notification_key(f"meeting:{source_id}:{canonical_identity(user_id)}:{mapping_hash}")


def sync_error_scope(source_id: str, mapping_hash: str) -> str:
    """수집 상태 알림은 사용자 배정과 무관하게 현재 설정 소스로 제한한다."""
    return notification_key(f"sync-error:{source_id}:{mapping_hash}")


def failure_rule_scope(source_id: str, mapping_hash: str, rule_id: str, target: str | None) -> str:
    """사용자가 등록한 수집 상태 규칙마다 독립적인 장애 구간을 추적한다."""
    return notification_key(f"failure-rule:{source_id}:{mapping_hash}:{rule_id}:{target or ''}")
