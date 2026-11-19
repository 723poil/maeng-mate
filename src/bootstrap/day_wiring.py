"""개인 근무 기록의 설정·전략·저장을 조립한다."""

from src.application.day_records import DayService
from src.application.work_records import WorkRecordsService
from src.bootstrap.clock import now
from src.bootstrap.environment import AppEnvironment
from src.domain.daily_work import FlexibleDeparturePolicy, WorkRules
from src.infrastructure.sqlite.connection import Database
from src.infrastructure.sqlite.day_records import SQLiteDayStore


def build_day_services(
    environment: AppEnvironment, rules: WorkRules
) -> tuple[DayService | None, WorkRecordsService | None]:
    """오늘 입력과 기록 정정에 같은 저장소·퇴근 계산 정책·시계를 주입한다."""
    if environment.database is None:
        return None, None
    policy = FlexibleDeparturePolicy()
    store = SQLiteDayStore(Database(environment.database), policy=policy, now=now)
    return DayService(store, policy, rules, now=now), WorkRecordsService(
        store, rules, now=now, policy=policy
    )
