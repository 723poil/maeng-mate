"""근무 설정 파일을 관리자 API와 근무 계산에 조립한다."""

from src.application.work_rules_settings import WorkRulesService
from src.application.work_settings import Settings
from src.bootstrap.environment import ConfigError
from src.bootstrap.instance import application_directory
from src.domain.daily_work import FlexibleDeparturePolicy
from src.infrastructure.sqlite.connection import Database
from src.infrastructure.sqlite.day_records import SQLiteDayStore
from src.infrastructure.work_rules_file import WorkRulesFile


def build_work_rules(settings: Settings, database: Database) -> WorkRulesService:
    """운영 파일이 없으면 기존 업무 설정과 기본 계산을 이어받는다."""
    try:
        store = SQLiteDayStore(database, policy=FlexibleDeparturePolicy())
        service = WorkRulesService(
            WorkRulesFile(
                application_directory() / "work-rules.json", apply_today=store.apply_today_rules
            ),
            settings.work_rules,
        )
        # 이전 버전 또는 중단된 저장에서 남은 오늘 기록과 미래 예약을 기동 시 한 번 맞춘다.
        with store.apply_today_rules(service.current_rules()):
            return service
    except (OSError, ValueError):
        raise ConfigError("근무 규칙 파일과 오늘 기록을 확인하세요.") from None
