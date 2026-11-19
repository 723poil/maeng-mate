"""조회 API와 정기 수집을 같은 프로세스에서 감독한다."""

import asyncio
import signal
import socket
from collections.abc import Awaitable, Callable, Iterator, Mapping
from contextlib import ExitStack, contextmanager, suppress
from typing import Protocol

import httpx
import uvicorn

from src.application.periodic import PeriodicCollector
from src.bootstrap.app_settings import application_environment
from src.bootstrap.backup_restore import guard_startup
from src.bootstrap.backup_transaction import pending_restore
from src.bootstrap.collection_wiring import build_collection
from src.bootstrap.environment import ConfigError, app_environment, required, sync_interval
from src.bootstrap.instance import application_directory
from src.bootstrap.notion_credentials import load_notion_environment
from src.bootstrap.runtime_settings import load_runtime_environment
from src.bootstrap.storage import require_migrated_database
from src.bootstrap.wiring import build_api
from src.bootstrap.work_rules import build_work_rules
from src.bootstrap.work_settings import load_work_settings
from src.infrastructure.sqlite.connection import Database
from src.infrastructure.sqlite.schedules import SQLiteSchedules


class ServerControl(Protocol):
    """구체 웹 서버와 무관하게 수명만 감독하는 경계다."""

    started: bool
    should_exit: bool

    async def serve(self) -> None:
        """서버가 종료될 때까지 실행한다."""
        ...


class ResidentServer(uvicorn.Server):
    """소켓과 종료 신호를 bootstrap이 관리하는 로컬 서버다."""

    def __init__(self, config: uvicorn.Config, listener: socket.socket) -> None:
        """바인딩을 마친 소켓만 서버에 전달한다."""
        super().__init__(config)
        self._listener = listener

    @contextmanager
    def capture_signals(self) -> Iterator[None]:
        """Uvicorn의 신호 재발송 대신 전체 작업을 정리한 후 종료한다."""
        yield

    async def serve(self, sockets: list[socket.socket] | None = None) -> None:
        """기존 소켓을 사용해 수집 시작 전에 바인딩 성공을 보장한다."""
        await super().serve(sockets=[self._listener] if sockets is None else sockets)


async def finish_collector(task: asyncio.Task[None], grace_period: float) -> None:
    """진행 중 수집을 기다리고 한도를 넘으면 취소 후 정리 결과를 회수한다."""
    try:
        await asyncio.wait_for(asyncio.shield(task), grace_period)
    except TimeoutError:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task


async def supervise(
    server: ServerControl,
    collect: Callable[[asyncio.Event], Awaitable[None]],
    stop: asyncio.Event,
    *,
    shutdown_timeout: float = 5.0,
) -> None:
    """어느 한 작업이 끝나도 나머지를 정리하고 실패를 숨기지 않는다."""
    api = asyncio.create_task(server.serve())
    collector: asyncio.Task[None] | None = None
    try:
        while not server.started:
            if api.done():
                await api
                raise RuntimeError("조회 API를 시작하지 못했습니다.")
            await asyncio.sleep(0.01)
        if stop.is_set() or api.done():
            return

        async def start_collection() -> None:
            """주입한 비동기 실행을 감독 가능한 작업으로 감싼다."""
            await collect(stop)

        collector = asyncio.create_task(start_collection())
        completed, _ = await asyncio.wait((api, collector), return_when=asyncio.FIRST_COMPLETED)
        if collector in completed:
            await collector
            if not stop.is_set():
                raise RuntimeError("정기 수집 작업이 예기치 않게 종료됐습니다.")
            server.should_exit = True
        await api
    finally:
        stop.set()
        server.should_exit = True
        # API 정리 실패 때문에 수집·HTTP 연결 정리가 생략되지 않게 한다.
        try:
            if collector is not None:
                await finish_collector(collector, shutdown_timeout)
        finally:
            await api


async def wait_for_restore_commit(committed: asyncio.Event, stop: asyncio.Event) -> None:
    """도우미가 교체를 확정한 뒤에만 조회·기록과 정기 수집을 허용한다."""
    home = application_directory()
    while not stop.is_set():
        if not pending_restore(home):
            committed.set()
            return
        # 도우미가 비정상 종료되면 복구를 요청하고 시험 서버를 종료해 저장 잠금을 돌려준다.
        guard_startup(allow_trial=True)
        await asyncio.sleep(0.2)


async def _run_resident(env: Mapping[str, str]) -> None:
    """준비된 개인 설정과 DB로만 API·수집을 명시적으로 시작한다."""
    env = application_environment(env)
    legacy_token = env.get("NOTION_TOKEN") or None
    env = load_notion_environment(load_runtime_environment(env), keep_api_available=True)
    environment = app_environment(env)
    interval = sync_interval(env)
    token = environment.notion_token
    path = required(env, "DATABASE_PATH")
    database = Database(path)
    await require_migrated_database(database)
    settings = load_work_settings(database, prepare_categories=True)
    stop = asyncio.Event()
    committed = asyncio.Event() if pending_restore(application_directory()) else None
    # 포트 충돌 시 예약 등록이나 원천 조회까지 진행하지 않는다.
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        try:
            # 직전 연결의 TIME_WAIT 때문에 정상 재시작이 막히지 않게 한다.
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            listener.bind(("127.0.0.1", environment.port))
            listener.listen(socket.SOMAXCONN)
            listener.setblocking(False)
        except OSError:
            raise ConfigError("로컬 API 포트를 사용할 수 없습니다: LOCAL_API_PORT") from None
        rules_service = build_work_rules(settings, database)
        schedules = SQLiteSchedules(database)
        sources = settings.active_sources
        async with httpx.AsyncClient(trust_env=False, follow_redirects=False) as client:
            collector = None
            if token and sources:
                await schedules.configure(sources, interval)
                reader, store = build_collection(
                    database, client, token, environment.notion_user_id, scheduled=True
                )
                collector = PeriodicCollector(sources, reader, store, schedules)
            app = build_api(
                environment,
                settings,
                notion_legacy_token=legacy_token,
                work_rules_service=rules_service,
                restore_committed=committed.is_set if committed is not None else None,
                periodic_status=collector.read_status if collector else None,
                desktop_periodic_status=collector.read_desktop_status if collector else None,
                update_interval=collector.set_interval if collector else None,
            )
            server = ResidentServer(
                uvicorn.Config(
                    app,
                    host="127.0.0.1",
                    port=environment.port,
                    access_log=False,
                    timeout_graceful_shutdown=5,
                ),
                listener,
            )
            loop = asyncio.get_running_loop()

            def request_stop() -> None:
                """신호 처리에서 새 수집과 API 요청을 함께 중지한다."""
                stop.set()
                server.should_exit = True

            with ExitStack() as cleanup:
                for sig in (signal.SIGINT, signal.SIGTERM):
                    previous = signal.getsignal(sig)
                    loop.add_signal_handler(sig, request_stop)
                    # 이벤트 루프 처리를 먼저 해제한 뒤 원래 프로세스 처리기를 되돌린다.
                    cleanup.callback(signal.signal, sig, previous)
                    cleanup.callback(loop.remove_signal_handler, sig)

                async def collect_when_ready(stop_event: asyncio.Event) -> None:
                    """시험 기동 중에는 외부 수집을 미루고 확정 후 일반 실행으로 전환한다."""
                    if committed is not None:
                        await wait_for_restore_commit(committed, stop_event)
                    if collector is None or stop_event.is_set():
                        await stop_event.wait()
                    else:
                        await collector.run(stop_event)

                await supervise(server, collect_when_ready, stop)


async def run_resident(env: Mapping[str, str]) -> None:
    """서버가 쓰는 DB의 경로를 실행 중에 바꾸지 못하게 한다."""
    from src.bootstrap.instance import coordination_lock

    with coordination_lock("storage", shared=True):
        await _run_resident(env)
