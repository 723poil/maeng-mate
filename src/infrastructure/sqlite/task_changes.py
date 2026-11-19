"""정상 수집 확정과 업무 변경 이력·알림을 원자적으로 연결한다."""

import json
import sqlite3
from collections.abc import Iterator, Sequence
from dataclasses import asdict
from datetime import datetime
from itertools import batched

from src.application.errors import QueryReadError, SnapshotError
from src.application.models import SyncRun
from src.application.notion_links import notion_link
from src.domain.models import SourceMapping, Task, WorkItem
from src.domain.task_changes import TaskChangePolicy, TaskFacts, task_facts
from src.domain.work_dates import canonical_identity
from src.infrastructure.sqlite.decoding import decode_item
from src.infrastructure.sqlite.notification_keys import notification_key, task_scope
from src.infrastructure.sqlite.serialization import fingerprint


def facts_hash(facts: TaskFacts) -> str:
    """관측 시각·제목·다른 담당자 순서가 기존 알림을 중복시키지 않게 한다."""
    return fingerprint(asdict(facts))


def task_from_payload(payload: str, source: SourceMapping) -> Task:
    """손상된 이전 자료를 없는 업무로 가장하지 않는다."""
    value: object = json.loads(payload)
    item = decode_item(value, source)
    if not isinstance(item, Task):
        raise QueryReadError()
    return item


def notification_body(title: str, description: str) -> str:
    """배너는 업무 제목과 실제 변경 이유만 짧게 표시한다."""
    return title[:90] + " · " + description


def outstanding_task_jobs(
    connection: sqlite3.Connection, source_id: str, user_key: str
) -> dict[str, list[sqlite3.Row]]:
    """현재 소스·사용자의 미전달 작업을 한 번 읽어 항목별로 묶는다."""
    rows = connection.execute(
        """SELECT event.item_id, event.after_hash, event.description,
                  job.id, job.state, job.body, job.notion_url
           FROM notification_jobs AS job JOIN task_changes AS event ON event.id=job.task_change_id
           WHERE job.kind='task' AND job.state IN ('pending','delivering')
             AND event.source_id=? AND event.user_key=?""",
        (source_id, user_key),
    )
    grouped: dict[str, list[sqlite3.Row]] = {}
    for row in rows:
        grouped.setdefault(row["item_id"], []).append(row)
    return grouped


def tasks_with_previous(
    connection: sqlite3.Connection, source_id: str, items: Sequence[WorkItem]
) -> Iterator[tuple[Task, sqlite3.Row | None]]:
    """이번 수집에 있는 업무의 이전 원문만 묶어서 읽고 입력 순서로 돌려준다."""
    tasks = (item for item in items if isinstance(item, Task))
    for batch in batched(tasks, 128):
        # 누락 후 돌아온 업무도 마지막 정상 값과 비교한다. 한 번에 보관하는 원문 수는 제한한다.
        placeholders = ",".join("?" for _ in batch)
        rows = connection.execute(
            f"""SELECT item_id,payload,mapping_hash FROM item_snapshots
            WHERE source_id=? AND item_id IN ({placeholders})""",
            (source_id, *(item.id for item in batch)),
        )
        previous = {row["item_id"]: row for row in rows}
        for item in batch:
            yield item, previous.get(item.id)


def record_task_changes(
    connection: sqlite3.Connection,
    run: SyncRun,
    items: Sequence[WorkItem],
    user_id: str | None,
    completed_at: str,
    previous_mapping: str | None,
    policy: TaskChangePolicy | None,
) -> None:
    """부분 수집·매핑 교체·본인 미설정은 변경 알림을 만들지 않는다."""
    if run.kind != "task" or user_id is None or policy is None:
        return
    user = canonical_identity(user_id)
    user_key = notification_key(user)
    scope = task_scope(run.source_id, user, run.mapping_hash)
    if previous_mapping != run.mapping_hash:
        connection.execute(
            "UPDATE notification_jobs SET state='obsolete' WHERE source_key=? AND kind='task' AND state IN ('pending','delivering')",
            (scope,),
        )
        return
    outstanding = outstanding_task_jobs(connection, run.source_id, user_key)
    now = datetime.fromisoformat(completed_at).timestamp()
    # 변경 판단용 복원에는 화면 칼럼이 필요 없으므로 소스당 같은 매핑을 사용한다.
    comparison_source = SourceMapping("task", run.source_id, {})
    for item, row in tasks_with_previous(connection, run.source_id, items):
        current_facts = task_facts(item, user)
        # 이전 매핑으로만 본 항목은 새 기준으로 삼되 오래된 실행권은 검사한다.
        comparable = row is None or row["mapping_hash"] == run.mapping_hash
        change = None
        if comparable:
            before = (
                task_from_payload(row["payload"], comparison_source) if row is not None else None
            )
            change = policy.compare(before, item, user)
        jobs = outstanding.get(item.id, ())
        if change is None and not jobs:
            continue
        current_hash = facts_hash(current_facts)
        for job in jobs:
            if job["after_hash"] != current_hash:
                connection.execute(
                    "UPDATE notification_jobs SET state='obsolete' WHERE id=?", (job["id"],)
                )
            elif comparable and change is None and job["state"] == "pending":
                # 이미 전달 중인 본문은 유지하고, 대기 중인 제목·주소만 실제로 바뀌면 갱신한다.
                body = notification_body(item.title, job["description"])
                url = notion_link(item.url)
                if body != job["body"] or url != job["notion_url"]:
                    connection.execute(
                        "UPDATE notification_jobs SET body=?,notion_url=? WHERE id=?",
                        (body, url, job["id"]),
                    )
        if change is None:
            continue
        identifier = notification_key(f"task-change:{run.source_id}:{item.id}:{user_key}:{run.id}")
        description = change.description()
        connection.execute(
            """INSERT INTO task_changes(id,source_id,item_id,run_id,user_key,mapping_hash,kinds,before_json,after_json,after_hash,title,description,observed_at)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                identifier,
                run.source_id,
                item.id,
                run.id,
                user_key,
                run.mapping_hash,
                json.dumps(change.kinds),
                json.dumps(asdict(change.before), ensure_ascii=False) if change.before else None,
                json.dumps(asdict(change.after), ensure_ascii=False),
                current_hash,
                item.title,
                description,
                completed_at,
            ),
        )
        if not change.after.assigned:
            continue  # 배정 해제는 이력만 남기며 알림을 보내지 않는다.
        if run.source is not None and not any(
            run.source.allows_event_alert(kind) for kind in change.kinds
        ):
            continue  # 저장 이력과 사용자가 등록한 발송 조건을 구분한다.
        connection.execute(
            """INSERT INTO notification_jobs(id,logical_key,kind,title,body,notion_url,due_at,expires_at,state,source_key,task_change_id)
            VALUES(?,?,'task',?,?,?,?,?,'pending',?,?)""",
            (
                identifier,
                identifier,
                "새 업무 배정" if "assigned" in change.kinds else "업무 변경",
                notification_body(item.title, description),
                notion_link(item.url),
                now,
                now + 86400,
                scope,
                identifier,
            ),
        )


def task_job_current(row: dict[str, object], user_id: str | None) -> bool:
    """발송 직전 현재 스냅샷의 본인·변경 사실·가용성을 함께 확인한다."""
    if (
        user_id is None
        or row["current_availability"] != "present"
        or row["change_mapping"] != row["current_mapping"]
    ):
        return False
    user = canonical_identity(user_id)
    if row["change_user"] != notification_key(user):
        return False
    payload, source = row["current_payload"], row["change_source"]
    if not isinstance(payload, str) or not isinstance(source, str):
        raise SnapshotError("storage_failed")
    try:
        current = task_from_payload(payload, SourceMapping("task", source, {}))
        facts = task_facts(current, user)
        return facts.assigned and facts_hash(facts) == row["change_after_hash"]
    except (ValueError, TypeError, KeyError):
        raise SnapshotError("storage_failed") from None
