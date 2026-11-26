"""격리된 실제 CLI에서 포트 충돌·시작 보정·종료를 수동 PDD로 확인한다."""

import argparse
import asyncio
import json
import os
import secrets
import signal
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
from dataclasses import replace
from datetime import datetime
from pathlib import Path

import httpx

from src.application.work_rules_settings import WorkRulesDocument
from src.domain.daily_work import DEFAULT_WORK_RULES, DayRecord, FlexibleDeparturePolicy
from src.domain.work_dates import SEOUL
from src.infrastructure.sqlite.connection import Database
from src.infrastructure.sqlite.day_records import SQLiteDayStore, decode_record
from src.infrastructure.sqlite.schema import migrate
from src.infrastructure.sqlite.work_settings import SQLiteWorkSettings
from src.infrastructure.work_rules_file import WorkRulesFile
from tests.fixtures.preferences import enable_notifications


def snapshot(path: Path) -> dict[str, list[tuple[object, ...]]]:
    """WAL 파일 모양 대신 모든 사용자 테이블의 논리 행을 비교한다."""
    with sqlite3.connect(path) as connection:
        tables = connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        ).fetchall()
        return {
            name: sorted(connection.execute(f'SELECT * FROM "{name}"').fetchall(), key=repr)
            for (name,) in tables
        }


def seed(path: Path, now: datetime) -> None:
    """카테고리 이전·오늘 규칙·기존 알림을 모두 보정할 가상 자료를 만든다."""
    database = Database(str(path))
    asyncio.run(migrate(database))
    SQLiteWorkSettings(database).initialize(
        {
            "version": 1,
            "timezone": "Asia/Seoul",
            "sources": [
                {
                    "kind": "task",
                    "dataSourceId": "fake-startup-source",
                    "properties": {"title": "t", "assignees": "a", "status": "s"},
                }
            ],
        }
    )
    enable_notifications(database, "departure_17")
    store = SQLiteDayStore(database, now=lambda: now, policy=FlexibleDeparturePolicy())
    asyncio.run(store.save(DayRecord(now.date(), attendance=now, rules=DEFAULT_WORK_RULES), 0))
    # 전달 행도 남겨 실패한 기동이 기존 발송 이력을 바꾸지 않는지 비교한다.
    with database.transaction() as connection:
        connection.execute(
            "INSERT INTO notification_deliveries(id,job_id,channel,kinds,state) "
            "SELECT 'fake-delivery',id,'macos','[\"departure_17\"]','pending' "
            "FROM notification_jobs LIMIT 1"
        )
    WorkRulesFile(path.parent / "app-support/work-rules.json").save(
        WorkRulesDocument(0, replace(DEFAULT_WORK_RULES, daily_minutes=450))
    )


def wait_ready(process: subprocess.Popen[bytes], client: httpx.Client) -> None:
    """실제 인증 API가 준비되거나 프로세스가 실패할 때까지 짧게 재확인한다."""
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        assert process.poll() is None, "격리 앱이 API 준비 전에 종료됐습니다."
        try:
            if client.get("/v1/status").status_code == 200:
                return
        except httpx.TransportError:
            pass
        time.sleep(0.02)
    raise AssertionError("격리 앱의 API 준비 시간이 초과됐습니다.")


def verify(command: str, directory: Path, *, baseline: bool) -> None:
    """실제 소켓·CLI·DB는 유지하고 개인 저장소와 외부 원천만 격리한다."""
    directory.mkdir()
    path = directory / "data.sqlite3"
    now = datetime.now(SEOUL).replace(hour=9, minute=0, second=0, microsecond=0)
    seed(path, now)
    token = secrets.token_urlsafe(32)
    processes: list[subprocess.Popen[bytes]] = []
    with socket.socket() as blocker:
        blocker.bind(("127.0.0.1", 0))
        blocker.listen()
        port = blocker.getsockname()[1]
        env = {
            "PATH": os.defpath,
            "PYTHONPATH": str(Path(__file__).resolve().parents[2]),
            "DATABASE_PATH": str(path),
            "LOCAL_API_PORT": str(port),
            "LOCAL_API_TOKEN": token,
            "FAKE_COMMAND": command,
            "FAKE_NOW": now.isoformat(),
        }

        def start() -> subprocess.Popen[bytes]:
            """개인 환경을 상속하지 않는 실제 개발 앱 하위 프로세스를 실행한다."""
            with (directory / f"process-{len(processes)}.log").open("wb") as output:
                process = subprocess.Popen(
                    [sys.executable, "-m", "tests.fixtures.resident"],
                    env=env,
                    stdout=output,
                    stderr=output,
                )
            processes.append(process)
            return process

        before = snapshot(path)
        conflicting = start()
        code = conflicting.wait(timeout=15)
        assert code != 0 if baseline else code == 1, (directory / "process-0.log").read_text()
        after = snapshot(path)
        changed = sorted(name for name in before if before[name] != after[name])
        print(
            json.dumps({"command": command, "exitCode": code, "conflictChangedTables": changed}),
            flush=True,
        )
        if baseline:
            assert "work_settings" in changed
            return
        assert after == before, changed

    try:
        with httpx.Client(
            base_url=f"http://127.0.0.1:{port}",
            headers={"Authorization": f"Bearer {token}"},
            trust_env=False,
            timeout=1,
        ) as client:
            for sig in (signal.SIGTERM, signal.SIGINT):
                process = start()
                wait_ready(process, client)
                assert client.get("/v1/status", headers={"Authorization": ""}).status_code == 401
                ready = snapshot(path)
                for name in (
                    "work_settings",
                    "day_records",
                    "day_record_revisions",
                    "notification_jobs",
                ):
                    assert ready[name] != before[name], name
                assert ready["notification_deliveries"] == before["notification_deliveries"]
                current_payload = ready["day_records"][0][2]
                assert isinstance(current_payload, str)
                current = decode_record(json.loads(current_payload))
                assert current.revision == 2 and current.attendance == now
                assert current.rules == replace(DEFAULT_WORK_RULES, daily_minutes=450)
                assert len(ready["day_record_revisions"]) == 2
                with sqlite3.connect(path) as connection:
                    jobs = connection.execute(
                        "SELECT revision,state FROM notification_jobs ORDER BY revision"
                    ).fetchall()
                    assert jobs == [(1, "obsolete"), (2, "pending")]
                assert not ready["sync_runs"] and not ready["sync_schedules"]
                assert client.get("/v1/day").status_code == 200
                assert snapshot(path) == ready, "조회가 저장 자료를 변경했습니다."
                process.send_signal(sig)
                # 단독 Uvicorn은 정상 정리 후 받은 신호를 재발송한다.
                assert process.wait(timeout=10) in (0, -sig)
                if sig == signal.SIGTERM:
                    first = ready
                else:
                    assert ready == first, "재시작이 보정을 중복 기록했습니다."
            # 보정 중 실패해도 확보한 포트와 역할 잠금이 다음 실행을 막지 않아야 한다.
            rules_path = directory / "app-support/work-rules.json"
            rules = rules_path.read_bytes()
            rules_path.write_bytes(b"invalid-json")
            failed = start()
            assert failed.wait(timeout=15) == 1
            with socket.socket() as probe:
                probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                probe.bind(("127.0.0.1", port))
                probe.listen()
            rules_path.write_bytes(rules)
            recovered = start()
            wait_ready(recovered, client)
            recovered.send_signal(signal.SIGTERM)
            assert recovered.wait(timeout=10) in (0, -signal.SIGTERM)
            assert snapshot(path) == first
        # 반복한 실제 기동으로 역할 잠금 해제를 확인한다. 단독 Uvicorn의 신호
        # 재발송은 finally 전에 프로세스를 종료할 수 있어 PID 파일 삭제는 요구하지 않는다.
        print(
            json.dumps({"command": command, "repairRestartSignalsRecovery": "passed"}), flush=True
        )
    finally:
        for process in processes:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)


def main() -> None:
    """사용자가 요청한 PDD만 실행하고 임시 앱 자료를 종료 시 제거한다."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--baseline", action="store_true")
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="maeng-startup-pdd-") as directory:
        for command in ("api",) if args.baseline else ("api", "run"):
            verify(command, Path(directory) / command, baseline=args.baseline)


if __name__ == "__main__":
    main()
