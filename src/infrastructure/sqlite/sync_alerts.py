"""수집 실패 구간마다 한 번만 알리는 영속 예약이다."""

import sqlite3

from src.application.models import SyncRun
from src.infrastructure.sqlite.notification_keys import notification_key, sync_error_scope


def enqueue_sync_failure(
    connection: sqlite3.Connection,
    run: SyncRun,
    finished_at: float,
    code: str,
    *,
    retrying: bool = False,
) -> None:
    """실패 기록 트랜잭션 안에서 직전 성공 이후의 같은 장애를 묶는다."""
    previous = connection.execute(
        """SELECT MAX(id) AS id FROM sync_runs
        WHERE source_id=? AND status='succeeded' AND id<?""",
        (run.source_id, run.id),
    ).fetchone()
    last_success = previous["id"] if previous is not None else None
    logical = notification_key(f"sync-error:{run.source_id}:{run.mapping_hash}:{last_success or 0}")
    body = {
        "storage_failed": "수집한 자료를 이 Mac에 저장하지 못했습니다.",
        "unauthorized": "설정 → Notion에서 토큰을 확인하세요.",
        "permission_denied": "Notion에서 이 연동이 원본 표에 접근할 수 있는지 확인하세요.",
        "not_found": "Notion에서 원본 표의 주소와 접근 권한을 확인하세요.",
        "mapping_invalid": "설정 → 수집 소스 → 연결 편집에서 Notion 칼럼을 확인하세요.",
        "request_invalid": "Notion 연결 설정을 확인하세요.",
        "invalid_response": "Notion이 보낸 응답을 읽지 못했습니다.",
        "pagination_invalid": "Notion에서 마지막 항목까지 불러오지 못했습니다.",
        "unavailable": "Notion에 연결하지 못했습니다.",
        "timeout": "Notion 응답을 기다리다 시간이 초과됐습니다.",
        "rate_limited": "Notion의 요청 한도에 걸렸습니다.",
    }.get(code, "수집 소스의 연결 상태를 확인하세요.")
    # 수동 수집에는 다음 예약이 없을 수 있으므로 실제 예약에 맞춰 안내한다.
    if retrying:
        body += " 잠시 후 다시 시도합니다."
    elif (
        code in {"unavailable", "timeout", "rate_limited"}
        and connection.execute(
            "SELECT 1 FROM sync_schedules WHERE source_id=?", (run.source_id,)
        ).fetchone()
        is not None
    ):
        body += " 다음 정기 수집 때 다시 시도합니다."
    connection.execute(
        """INSERT INTO notification_jobs(id,logical_key,kind,title,body,due_at,expires_at,state,
            source_key,sync_source_id,sync_mapping_hash,sync_failure_run_id)
        VALUES(?,?,'sync_error','수집 실패',?,?,?,'pending',?,?,?,?) ON CONFLICT(id) DO UPDATE SET body=excluded.body WHERE notification_jobs.state='pending'""",
        (
            logical,
            logical,
            body,
            finished_at,
            finished_at + 3600,
            sync_error_scope(run.source_id, run.mapping_hash),
            run.source_id,
            run.mapping_hash,
            run.id,
        ),
    )


def resolve_sync_failure(connection: sqlite3.Connection, source_id: str) -> None:
    """정상 스냅샷 확정과 함께 복구된 소스의 미전달 오류 알림을 멈춘다."""
    connection.execute(
        """UPDATE notification_jobs SET state='obsolete' WHERE kind='sync_error'
        AND sync_source_id=? AND state IN ('pending','delivering')""",
        (source_id,),
    )
