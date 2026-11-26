"""실제 SQLite·인증 HTTP 경로에서 작업자 소진·대기·실행권 보존을 확인한다."""

import asyncio
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

import pytest

from src.application.notifications import DeliveryResult, NotificationJob, NotificationRunner
from src.bootstrap.notification_worker import NotificationWorker
from tests.fixtures.notification_delivery import CHANNELS, delivery_environment


def wait_until(condition: Callable[[], bool], *, timeout: float = 3) -> None:
    """고정 수면 대신 실제 저장 완료를 제한 시간 안에서 관찰한다."""
    deadline = time.monotonic() + timeout
    while not condition():
        assert time.monotonic() < deadline, "전달 완료를 확인하지 못했습니다."
        time.sleep(0.01)


def test_worker_drains_real_queue_then_waits() -> None:
    """12개 채널을 연속 처리하고 빈 큐에서는 추가 claim 없이 대기한다."""
    with delivery_environment() as environment:
        environment.seed()
        worker = NotificationWorker(
            NotificationRunner(environment.queue, dict.fromkeys(CHANNELS, environment.sender))
        )
        with (
            patch(
                "src.bootstrap.notification_worker.application_directory",
                return_value=environment.root,
            ),
            patch.object(environment.queue, "claim", wraps=environment.queue.claim) as claim,
        ):
            worker.start()
            try:
                wait_until(lambda: claim.call_count == 13)
                time.sleep(0.15)
                assert claim.call_count == 13
                assert len(environment.sender.calls) == 12
                assert environment.sender.calls[-1][2] - environment.sender.calls[0][2] < 5
                assert environment.states() == [
                    {"channel": channel, "state": "requested", "count": 6} for channel in CHANNELS
                ]
                assert worker.status() is None
            finally:
                started = time.monotonic()
                worker.close()
                assert time.monotonic() - started < 1
        # 같은 실행권은 재시작 후에도 재전송하지 않는다.
        restarted = NotificationRunner(
            environment.queue, dict.fromkeys(CHANNELS, environment.sender)
        )
        assert not restarted.run_once().processed
        assert len(environment.sender.calls) == 12


@pytest.mark.parametrize("result", ["failed", "unknown"])
def test_worker_waits_after_channel_failure(result: DeliveryResult) -> None:
    """실패를 저장한 뒤 5초 대기하고 다음 별도 채널만 처리한다."""
    with delivery_environment() as environment:
        environment.seed(1)
        environment.sender.result = result
        worker = NotificationWorker(
            NotificationRunner(environment.queue, dict.fromkeys(CHANNELS, environment.sender))
        )
        with patch(
            "src.bootstrap.notification_worker.application_directory", return_value=environment.root
        ):
            worker.start()
            try:
                wait_until(lambda: worker.status() == "알림 요청 확인 필요")
                assert len(environment.sender.calls) == 1
                time.sleep(0.15)
                assert len(environment.sender.calls) == 1
                wait_until(lambda: len(environment.sender.calls) == 2, timeout=6)
                assert environment.sender.calls[1][2] - environment.sender.calls[0][2] >= 5
                wait_until(
                    lambda: (
                        sum(
                            row["count"]
                            for row in environment.states()
                            if row["state"] == result and isinstance(row["count"], int)
                        )
                        == 2
                    )
                )
                assert all(row["state"] == result for row in environment.states())
            finally:
                worker.close()


def test_authorization_cancellation_does_not_delay_next_delivery() -> None:
    """첫 승인 직전에 실행권을 무효화해도 다음 유효한 전달을 처리한다."""
    with delivery_environment() as environment:
        environment.seed(1)
        authorize = environment.queue.authorize
        first = True

        def revoke_first(job: NotificationJob) -> bool:
            """실제 승인 전에 첫 채널만 무효화해 결과 저장과 다음 전달을 확인한다."""
            nonlocal first
            if first:
                first = False
                with environment.database.transaction() as connection:
                    connection.execute(
                        "UPDATE notification_deliveries SET revoked=1 WHERE id=?", (job.id,)
                    )
            return authorize(job)

        with patch.object(environment.queue, "authorize", revoke_first):
            runner = NotificationRunner(
                environment.queue, dict.fromkeys(CHANNELS, environment.sender)
            )
            cancelled = runner.run_once()
            delivered = runner.run_once()
        assert cancelled.processed and not cancelled.failed
        assert delivered.processed and not delivered.failed
        assert len(environment.sender.calls) == 1
        assert sorted(row["state"] for row in environment.states()) == ["cancelled", "requested"]


def test_muted_head_does_not_hide_next_due_job() -> None:
    """채널 없는 선두 예약을 정리하고 같은 claim에서 다음 유효한 예약을 반환한다."""
    with delivery_environment() as environment:
        environment.seed(3, kind="departure", prefix="muted")
        environment.seed(1, prefix="valid")
        (job,) = environment.queue.claim()
        assert job.title == "가상 알림" and environment.queue.authorize(job)
        rows = asyncio.run(
            environment.database.fetch(
                "SELECT state,COUNT(*) AS count FROM notification_jobs GROUP BY state ORDER BY state"
            )
        )
        assert rows == [{"state": "cancelled", "count": 3}, {"state": "delivering", "count": 1}]


def test_settings_change_after_claim_revokes_both_channels() -> None:
    """실제 구독 저장이 이미 claim한 채널과 대기 채널을 함께 차단한다."""
    with delivery_environment() as environment:
        environment.seed(1)
        (job,) = environment.queue.claim()
        environment.mute()
        assert not environment.queue.authorize(job)
        environment.queue.ack(job, "cancelled")
        assert not environment.queue.claim()
        assert all(row["state"] == "cancelled" for row in environment.states())


def test_ack_loss_waits_without_resending() -> None:
    """ACK 응답이 유실돼도 확정된 요청은 재전송하지 않고 연결 장애로 대기한다."""
    with delivery_environment() as environment:
        environment.seed(1)
        acknowledge = environment.queue.ack

        def lose_response(job: NotificationJob, result: DeliveryResult) -> None:
            """DB 확정 뒤 클라이언트가 응답을 잃는 경계를 만든다."""
            acknowledge(job, result)
            raise OSError("가상 응답 유실")

        worker = NotificationWorker(
            NotificationRunner(environment.queue, dict.fromkeys(CHANNELS, environment.sender))
        )
        with (
            patch(
                "src.bootstrap.notification_worker.application_directory",
                return_value=environment.root,
            ),
            patch.object(environment.queue, "ack", lose_response),
        ):
            worker.start()
            try:
                wait_until(lambda: worker.status() == "알림 연결 확인 필요")
                time.sleep(0.15)
                assert len(environment.sender.calls) == 1
            finally:
                worker.close()
        runner = NotificationRunner(environment.queue, dict.fromkeys(CHANNELS, environment.sender))
        assert runner.run_once().processed
        assert not runner.run_once().processed
        assert len({call[0] for call in environment.sender.calls}) == 2


def test_restore_and_stop_prevent_new_claims() -> None:
    """복원 보류 중에는 전달을 시작하지 않고 대기 중 종료도 즉시 반영한다."""
    with delivery_environment() as environment:
        environment.seed(1)
        (environment.root / ".restore-pending").mkdir()
        worker = NotificationWorker(
            NotificationRunner(environment.queue, dict.fromkeys(CHANNELS, environment.sender))
        )
        with (
            patch(
                "src.bootstrap.notification_worker.application_directory",
                return_value=environment.root,
            ),
            patch.object(environment.queue, "claim", wraps=environment.queue.claim) as claim,
        ):
            worker.start()
            time.sleep(0.2)
            worker.close()
            assert claim.call_count == 0
        assert not environment.sender.calls


def test_stop_during_delivery_does_not_claim_next() -> None:
    """진행 중 요청은 ACK하고 종료 신호 뒤에는 다음 채널을 claim하지 않는다."""
    with delivery_environment() as environment:
        environment.seed(1)
        release = threading.Event()

        worker = NotificationWorker(
            NotificationRunner(environment.queue, dict.fromkeys(CHANNELS, environment.sender))
        )
        with (
            patch(
                "src.bootstrap.notification_worker.application_directory",
                return_value=environment.root,
            ),
        ):
            # 원래 메서드를 보존해 가상 송신의 재귀 호출을 피한다.
            record = type(environment.sender).request

            def record_and_wait(job: NotificationJob) -> DeliveryResult:
                """외부 요청 대기를 끝낸 뒤 한 번만 결과를 기록한다."""
                record(environment.sender, job)
                assert release.wait(3)
                return "requested"

            with patch.object(environment.sender, "request", record_and_wait):
                worker.start()
                assert environment.sender.called.wait(3)
                closing = threading.Thread(target=worker.close)
                closing.start()
                wait_until(worker._stop.is_set)
                release.set()
                closing.join(timeout=2)
                assert not closing.is_alive()
        assert len(environment.sender.calls) == 1
        assert sorted(row["state"] for row in environment.states()) == ["pending", "requested"]


def test_restore_started_during_drain_blocks_next_delivery() -> None:
    """연속 전달 중 복원 표식이 생겨도 다음 실행권을 요청하지 않는다."""
    with delivery_environment() as environment:
        environment.seed(1)
        record = environment.sender.request

        def start_restore(job: NotificationJob) -> DeliveryResult:
            """첫 요청 뒤 복원 보류를 실제 파일로 표시한다."""
            result = record(job)
            (environment.root / ".restore-pending").mkdir()
            return result

        worker = NotificationWorker(
            NotificationRunner(environment.queue, dict.fromkeys(CHANNELS, environment.sender))
        )
        with (
            patch(
                "src.bootstrap.notification_worker.application_directory",
                return_value=environment.root,
            ),
            patch.object(environment.sender, "request", start_restore),
        ):
            worker.start()
            try:
                assert environment.sender.called.wait(3)
                time.sleep(0.15)
                assert len(environment.sender.calls) == 1
            finally:
                worker.close()
        assert sorted(row["state"] for row in environment.states()) == ["pending", "requested"]


def test_sender_exception_is_saved_as_unknown() -> None:
    """외부 요청 여부가 불명확한 예외는 unknown으로 보존하고 재발송하지 않는다."""
    with delivery_environment() as environment:
        environment.seed(1)
        runner = NotificationRunner(environment.queue, dict.fromkeys(CHANNELS, environment.sender))
        with patch.object(environment.sender, "request", side_effect=OSError("가상 요청 유실")):
            result = runner.run_once()
        assert result.processed and result.failed
        assert runner.run_once().processed
        assert not runner.run_once().processed
        assert len(environment.sender.calls) == 1
        assert sorted(row["state"] for row in environment.states()) == ["requested", "unknown"]


def test_concurrent_http_claims_keep_channel_tokens_unique() -> None:
    """동시 HTTP 요청에도 채널마다 한 실행권만 획득한다."""
    with delivery_environment() as environment:
        environment.seed(1)
        with ThreadPoolExecutor(max_workers=4) as executor:
            results = list(executor.map(lambda _: environment.queue.claim(), range(4)))
        jobs = [job for claimed in results for job in claimed]
        assert len(jobs) == 2
        assert len({job.id for job in jobs}) == 2
        assert len({job.token for job in jobs}) == 2
        for job in jobs:
            assert environment.queue.authorize(job)
            environment.queue.ack(job, "requested")
        assert not environment.queue.claim()


def test_slack_server_result_survives_late_client_ack() -> None:
    """서버가 먼저 기록한 Slack 결과를 늦은 클라이언트 ACK가 덮지 못한다."""
    with delivery_environment() as environment:
        environment.seed(1)
        jobs = [environment.queue.claim()[0], environment.queue.claim()[0]]
        job = next(job for job in jobs if job.channel == "slack")
        reserved = asyncio.run(environment.store.reserve_slack(job.id, job.token))
        assert reserved is not None
        assert asyncio.run(environment.store.reserve_slack(job.id, job.token)) is None
        asyncio.run(environment.store.complete_slack(job.id, job.token, "requested"))
        environment.queue.ack(job, "unknown")
        assert (
            next(row for row in environment.states() if row["channel"] == "slack")["state"]
            == "requested"
        )
