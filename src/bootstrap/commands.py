"""수동 저장 명령에 필요한 구현과 설정을 조립한다."""

from collections.abc import Mapping

from src.bootstrap.app_settings import data_environment
from src.bootstrap.clock import now
from src.bootstrap.environment import ConfigError, database_path, required
from src.bootstrap.notion_credentials import load_notion_environment
from src.bootstrap.runtime_settings import load_runtime_environment
from src.bootstrap.work_settings import load_work_settings


async def _run_storage(command: str, env: Mapping[str, str]) -> int:
    """수동 수집만 외부 원천과 DB에 연결하고 자동 작업은 생성하지 않는다."""
    import httpx

    from src.application.ports import collection_readers
    from src.application.use_cases.synchronize import synchronize
    from src.bootstrap.collection_wiring import build_collection
    from src.infrastructure.sqlite.connection import Database
    from src.infrastructure.sqlite.schema import migrate

    env = data_environment(env)
    database = Database(database_path(required(env, "DATABASE_PATH")))
    if command == "migrate":
        await migrate(database)
        print("스냅샷 DB 준비 완료")
        return 0
    env = load_notion_environment(load_runtime_environment(env))
    token = required(env, "NOTION_TOKEN")
    from src.bootstrap.storage import require_migrated_database

    await require_migrated_database(database)
    settings = load_work_settings(database, prepare_categories=True)
    if not settings.active_sources:
        raise ConfigError("수집할 소스에 카테고리를 먼저 지정하세요.")
    summary = {"succeeded": 0, "failed": 0, "skipped": 0}
    async with httpx.AsyncClient(trust_env=False, follow_redirects=False) as client:
        reader, store = build_collection(
            database, client, token, env.get("NOTION_USER_ID") or None, scheduled=False
        )
        for source, source_reader, started in collection_readers(
            settings.active_sources, reader, now
        ):
            result = await synchronize(source, source_reader, store, now, started_at=started)
            summary[result["status"]] += 1
    print(
        f"수집 결과: 성공 {summary['succeeded']}, 실패 {summary['failed']}, 실행 중 {summary['skipped']}"
    )
    return int(summary["failed"] > 0 or summary["skipped"] > 0)


async def run_storage(command: str, env: Mapping[str, str]) -> int:
    """수동 수집·스키마 준비도 데이터 위치 전환 중에는 실행하지 않는다."""
    from src.bootstrap.instance import coordination_lock

    with coordination_lock("storage", shared=True):
        return await _run_storage(command, env)
