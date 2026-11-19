"""현재 설정의 정상 수집 근거와 자료 신선도를 표현한다."""

from datetime import UTC, datetime, timedelta

from src.application.errors import QueryReadError
from src.application.today_models import SourceSnapshot


def utc_string(value: datetime) -> str:
    """API와 MCP에서 같은 UTC 시점 표현을 사용한다."""
    return value.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def source_metadata(
    snapshot: SourceSnapshot, now: datetime, max_age_seconds: int
) -> dict[str, object]:
    """빈 정상 자료와 미수집·실패·매핑 변경을 구별한다."""
    reasons = []
    fresh_until: datetime | None = None
    timestamp = snapshot.last_success_at
    if timestamp is None:
        reasons.append("source_not_collected")
    else:
        age = (now - timestamp).total_seconds()
        if age < 0:
            reasons.append("source_timestamp_in_future")
        else:
            try:
                # 실제 저장 예약을 사용해야 주기 변경·재실행 후에도 정상 대기를 지연으로 오인하지 않는다.
                scheduled = snapshot.next_scheduled_at
                fresh_until = (
                    scheduled + timedelta(minutes=5)
                    if scheduled is not None and scheduled >= timestamp
                    else timestamp + timedelta(seconds=max_age_seconds)
                )
            except OverflowError:
                raise QueryReadError() from None
            if now >= fresh_until:
                reasons.append("source_expired")
        if not snapshot.mapping_matches:
            reasons.append("source_mapping_changed")
    if snapshot.latest_run_status in {"failed", "abandoned"}:
        reasons.append("last_sync_failed")
    return {
        "sourceId": snapshot.source_id,
        "kind": snapshot.kind,
        "lastSuccessfulSyncAt": utc_string(timestamp) if timestamp else None,
        "nextScheduledSyncAt": utc_string(snapshot.next_scheduled_at)
        if snapshot.next_scheduled_at
        else None,
        "freshUntil": utc_string(fresh_until) if fresh_until else None,
        "latestRunStatus": snapshot.latest_run_status,
        "stale": bool(reasons),
        "reasons": reasons,
    }
