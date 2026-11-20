"""업무 설정 이전 순서와 이전 후 문서 버전을 결정한다."""

from dataclasses import dataclass, field

from src.application.alert_schema_upgrade import upgrade_alert_schema
from src.application.category_configuration_upgrade import (
    upgrade_category_configuration,
    upgrade_category_event_alerts,
)
from src.application.category_upgrade import upgrade_category_preferences, upgrade_legacy_categories
from src.application.main_list_upgrade import upgrade_main_list_rules
from src.application.work_settings import (
    Settings,
    WorkSettingsDocument,
    is_initial_document,
    parse_settings,
)
from src.domain.preferences import Preferences


@dataclass(frozen=True)
class WorkSettingsUpgrade:
    """함께 저장할 업무 문서와 이전 화면 그룹의 연결 정보다."""

    document: WorkSettingsDocument
    category_keys: dict[str, str] | None = field(repr=False)


def settings_up_to_date(document: WorkSettingsDocument, settings: Settings) -> bool:
    """모든 이전이 끝난 문서는 쓰기 잠금 없이 조회를 마친다."""
    payload = document.payload
    categories_ready = payload.get("categoryAssignmentVersion") == 1 or not payload.get("sources")
    return (
        categories_ready
        and payload.get("mainListRulesVersion") == 1
        and all(
            item.configuration is not None and item.configuration.event_alerts == ()
            for item in settings.categories
        )
    )


def upgrade_work_settings(
    current: WorkSettingsDocument, preferences: Preferences
) -> WorkSettingsUpgrade | None:
    """카테고리 배정·칼럼·이벤트·메인 목록 순서로 한 버전의 결과를 만든다."""
    assignment = upgrade_legacy_categories(current.payload)
    payload, keys = assignment if assignment is not None else (current.payload, None)
    columns = upgrade_category_configuration(payload)
    if columns is not None:
        payload = columns
    events = upgrade_category_event_alerts(payload)
    if events is not None:
        payload = events
    # 옛 표시 선택은 앞 단계에서 사라질 수 있어 최초 원문도 함께 전달한다.
    main_list = upgrade_main_list_rules(payload, current.payload)
    if main_list is not None:
        payload = main_list
    selected = (
        upgrade_category_preferences(preferences, keys, parse_settings(payload))
        if keys
        else preferences
    )
    schema = upgrade_alert_schema(payload, selected)
    if schema is not None:
        payload = schema
    if (
        assignment is None
        and columns is None
        and events is None
        and main_list is None
        and schema is None
    ):
        return None
    # 빈 초기 문서는 내부 표식만 추가해 최초 파일 가져오기 자격을 유지한다.
    revision = current.revision if is_initial_document(current) else current.revision + 1
    return WorkSettingsUpgrade(WorkSettingsDocument(revision, payload), keys)
