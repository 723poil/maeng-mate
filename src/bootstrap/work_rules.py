"""근무 설정 파일을 관리자 API와 근무 계산에 조립한다."""

from src.application.work_rules_settings import WorkRulesService
from src.application.work_settings import Settings
from src.bootstrap.environment import ConfigError
from src.bootstrap.instance import application_directory
from src.infrastructure.work_rules_file import WorkRulesFile


def build_work_rules(settings: Settings) -> WorkRulesService:
    """운영 파일이 없으면 기존 업무 설정과 기본 계산을 이어받는다."""
    try:
        return WorkRulesService(
            WorkRulesFile(application_directory() / "work-rules.json"), settings.work_rules
        )
    except (OSError, ValueError):
        raise ConfigError("work-rules.json의 형식과 권한을 확인하세요.") from None
