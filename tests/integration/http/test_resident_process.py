"""실제 상주 프로세스의 재시작·인증·신호 정리를 검증한다."""

import asyncio
import json
import os
import signal
import socket
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from pathlib import Path

from src.infrastructure.sqlite.connection import Database
from src.infrastructure.sqlite.schema import migrate
from src.infrastructure.sqlite.work_settings import SQLiteWorkSettings

TOKEN = "fake-process-test-token-" * 3


def read_status(port: int) -> dict[str, object]:
    """로컬 인증 API만 호출하고 응답 타입을 확인한다."""
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}/v1/status", headers={"Authorization": f"Bearer {TOKEN}"}
    )
    with urllib.request.urlopen(request, timeout=2) as response:
        value: object = json.load(response)
    assert isinstance(value, dict)
    return value


def wait_status(
    process: subprocess.Popen[bytes], port: int, predicate: Callable[[dict[str, object]], bool]
) -> dict[str, object]:
    """고정 대기 대신 상태 조건이 충족될 때까지 제한 시간 안에서 기다린다."""
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        assert process.poll() is None, "상주 프로세스가 조기 종료했습니다."
        try:
            status = read_status(port)
            periodic = status.get("periodicSync")
            if isinstance(periodic, dict) and predicate(periodic):
                return periodic
        except (urllib.error.URLError, ConnectionError, TimeoutError):
            pass
        time.sleep(0.02)
    raise AssertionError("상주 API 준비 시간이 초과됐습니다.")


def rows(database: Path, query: str) -> list[tuple[object, ...]]:
    """테스트 전용 파일의 실행 기록만 읽는다."""
    with sqlite3.connect(database) as connection:
        return connection.execute(query).fetchall()


def test_run_process_restart_shutdown_and_port_conflict(tmp_path: Path) -> None:
    """재시작 시 포트·예약을 재사용하고 SIGTERM 취소를 DB에 정리한다."""
    database = tmp_path / "work.sqlite3"
    store = Database(str(database))
    asyncio.run(migrate(store))
    SQLiteWorkSettings(store).initialize(
        {
            "version": 1,
            "timezone": "Asia/Seoul",
            "sources": [
                {
                    "kind": "task",
                    "dataSourceId": name,
                    "properties": {"title": "t", "assignees": "a", "status": "s"},
                }
                for name in ("fake-one", "fake-two")
            ],
        }
    )
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    # 부모의 개인 환경이나 인증값을 하위 프로세스에 전달하지 않는다.
    env = {
        "PATH": os.defpath,
        "PYTHONPATH": str(Path(__file__).resolve().parents[3]),
        "LOCAL_API_TOKEN": TOKEN,
        "LOCAL_API_PORT": str(port),
        "DATABASE_PATH": str(database),
        "NOTION_TOKEN": "fake-notion",
        "SYNC_INTERVAL_SECONDS": "300",
    }
    processes: list[subprocess.Popen[bytes]] = []

    def start(*, slow: bool = False) -> subprocess.Popen[bytes]:
        """출력은 개인 자료가 없는 테스트 로그 파일로 제한한다."""
        with (tmp_path / f"process-{len(processes)}.log").open("wb") as output:
            process = subprocess.Popen(
                [sys.executable, "-m", "tests.fixtures.resident"],
                env={**env, "FAKE_SLOW_SOURCE": str(int(slow))},
                stdout=output,
                stderr=output,
            )
        processes.append(process)
        return process

    def stop(process: subprocess.Popen[bytes]) -> None:
        """정상 신호 종료가 취소 정리와 함께 성공하는지 확인한다."""
        process.send_signal(signal.SIGTERM)
        assert process.wait(timeout=9) == 0

    try:
        first = start()
        wait_status(first, port, lambda state: state.get("succeededSources") == 2)
        before = rows(database, "SELECT * FROM sync_runs")
        read_status(port)
        assert rows(database, "SELECT * FROM sync_runs") == before
        stop(first)
        # 직전 TCP 연결의 TIME_WAIT 상태에서도 즉시 같은 포트로 시작해야 한다.
        restarted = start()
        wait_status(restarted, port, lambda state: state.get("enabled") is True)
        assert rows(database, "SELECT * FROM sync_runs") == before
        stop(restarted)
        with sqlite3.connect(database) as connection:
            connection.execute("UPDATE sync_schedules SET next_run_at=0")
        slow = start(slow=True)
        wait_status(slow, port, lambda state: state.get("runningSources") == 1)
        stop(slow)
        assert rows(database, "SELECT COUNT(*) FROM sync_runs WHERE status='running'") == [(0,)]
        assert rows(database, "SELECT COUNT(*) FROM sync_runs WHERE status='failed'") == [(1,)]
        assert rows(
            database, "SELECT COUNT(*) FROM work_source_state WHERE lease_until IS NOT NULL"
        ) == [(0,)]
        before = rows(database, "SELECT * FROM sync_runs")
        with socket.socket() as blocker:
            blocker.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            blocker.bind(("127.0.0.1", port))
            blocker.listen()
            conflicting = start()
            assert conflicting.wait(timeout=5) == 1
        assert rows(database, "SELECT * FROM sync_runs") == before
    finally:
        for process in processes:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=3)
