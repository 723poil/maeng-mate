"""파일·DB와 무관하게 업무 설정 JSON의 구조와 원천 매핑을 검증한다."""

from collections.abc import Mapping
from dataclasses import dataclass, field, replace

from src.application.category_settings_codec import category_documents as category_documents
from src.application.category_settings_codec import (
    legacy_categories,
    parse_categories,
    resolve_category,
)
from src.application.settings_validation import WorkSettingsError as WorkSettingsError
from src.application.settings_validation import nonblank
from src.application.source_settings_codec import (
    parse_source,
    read_completed_statuses,
    read_display,
    resolve_category_mapping,
)
from src.domain.daily_work import WorkRules, parse_rules
from src.domain.models import SourceMapping
from src.domain.source_display import SourceCategory, SourceDisplay
from src.domain.task_display import QATaskDisplayPolicy


@dataclass(frozen=True)
class Settings:
    """검증된 소스 매핑과 보관 정책 확인 상태다."""

    sources: tuple[SourceMapping, ...]
    retention_confirmed: bool = False
    history_days: int = 90
    log_days: int = 14
    completed_statuses: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    work_rules: WorkRules | None = None
    source_pages: Mapping[str, str] = field(default_factory=dict)
    qa_display_policy: QATaskDisplayPolicy = field(default_factory=QATaskDisplayPolicy)
    display: tuple[SourceDisplay, ...] = ()
    categories: tuple[SourceCategory, ...] = ()

    @property
    def source_categories(self) -> dict[str, SourceCategory]:
        """소스별 현재 카테고리를 이름 변경과 독립된 ID로 연결한다."""
        return {item.id: item.category for item in self.display if item.category is not None}

    @property
    def active_sources(self) -> tuple[SourceMapping, ...]:
        """등록 카테고리에 연결된 소스만 수집과 조회에 사용한다."""
        active = self.source_categories
        return tuple(source for source in self.sources if source.source_id in active)


def parse_settings(value: object) -> Settings:
    """외부 JSON을 검증하고 오류에 설정 원문을 포함하지 않는다."""
    try:
        if not isinstance(value, dict) or value.keys() - {
            "version",
            "timezone",
            "sources",
            "categories",
            "categoryAssignmentVersion",
            "classificationVersion",
            "mainListRulesVersion",
            "retention",
            "workRules",
            "qaDisplay",
        }:
            raise ValueError
        # 기존 설정의 버전 비교와 선택 필드의 null 허용 범위를 유지한다.
        rules_version = value.get("mainListRulesVersion", 1)
        if (
            value.get("version") != 1
            or value.get("timezone") != "Asia/Seoul"
            or value.get("categoryAssignmentVersion") not in (None, 1)
            or type(value.get("classificationVersion", 1)) is not int
            or value.get("classificationVersion", 1) not in (1, 2, 3, 4)
            or type(rules_version) is not int
            or rules_version != 1
        ):
            raise ValueError
        raw_sources = source_documents(value)
        sources = tuple(parse_source(item) for item in raw_sources)
        if len({source.source_id for source in sources}) != len(sources):
            raise ValueError
        completed = {
            source.source_id: read_completed_statuses(raw)
            for source, raw in zip(sources, raw_sources, strict=True)
            if source.kind == "task" and "completedStatuses" in raw
        }
        work_rules = parse_rules(value.get("workRules"))
        qa_display = read_qa_display(value.get("qaDisplay"))
        source_pages = {
            source.source_id: nonblank(raw["pageUrl"])
            for source, raw in zip(sources, raw_sources, strict=True)
            if "pageUrl" in raw
        }
        categories = (
            parse_categories(value["categories"])
            if value.get("categories") is not None
            else legacy_categories(raw_sources)
        )
        by_id = {category.id: category for category in categories}
        rule_ids = [rule.id for source in sources for rule in source.alert_rules]
        rule_ids.extend(
            entry.rule.id
            for category in categories
            if category.configuration
            for entry in category.configuration.alerts
        )
        if len(set(rule_ids)) != len(rule_ids):
            raise ValueError
        assignments = tuple(resolve_category(raw, by_id) for raw in raw_sources)
        sources = tuple(
            resolve_category_mapping(source, raw, category)
            for source, raw, category in zip(sources, raw_sources, assignments, strict=True)
        )
        display = tuple(
            read_display(source, raw, category)
            for source, raw, category in zip(sources, raw_sources, assignments, strict=True)
        )
        settings = Settings(
            sources,
            completed_statuses=completed,
            work_rules=work_rules,
            source_pages=source_pages,
            qa_display_policy=qa_display,
            display=display,
            categories=categories,
        )
        retention = value.get("retention")
        if retention is None:
            return settings
        if not isinstance(retention, dict) or set(retention) != {
            "historyDays",
            "logDays",
            "confirmed",
        }:
            raise ValueError
        days, logs, confirmed = (
            retention["historyDays"],
            retention["logDays"],
            retention["confirmed"],
        )
        if type(days) is not int or type(logs) is not int or type(confirmed) is not bool:
            raise ValueError
        if not 1 <= days <= 3650 or not 1 <= logs <= 3650:
            raise ValueError
        return replace(settings, retention_confirmed=confirmed, history_days=days, log_days=logs)
    except (ValueError, TypeError, KeyError):
        raise WorkSettingsError("invalid") from None


def read_qa_display(value: object) -> QATaskDisplayPolicy:
    """QA 노출 기간과 종료 항목을 숨길 차시를 개인 설정으로 분리한다."""
    if value is None:
        return QATaskDisplayPolicy()
    required = {"completedRetentionDays", "hiddenCompletedRounds"}
    if (
        not isinstance(value, dict)
        or not required <= value.keys()
        or value.keys() - required - {"hiddenStatuses"}
    ):
        raise ValueError
    days, rounds = value["completedRetentionDays"], value["hiddenCompletedRounds"]
    if type(days) is not int or not 0 <= days <= 3650 or not isinstance(rounds, list):
        raise ValueError
    hidden = value.get("hiddenStatuses", [])
    if not isinstance(hidden, list):
        raise ValueError
    return QATaskDisplayPolicy(
        days, tuple(nonblank(name) for name in rounds), tuple(nonblank(status) for status in hidden)
    )


@dataclass(frozen=True)
class WorkSettingsDocument:
    """원문을 repr에서 제외한 버전 있는 JSON 설정이다."""

    revision: int
    payload: dict[str, object] = field(repr=False)


def empty_work_settings() -> dict[str, object]:
    """새 설치에서만 사용하는 미설정 문서를 매번 새 값으로 만든다."""
    return {
        "version": 1,
        "timezone": "Asia/Seoul",
        "sources": [],
        "mainListRulesVersion": 1,
        "classificationVersion": 4,
    }


def is_initial_document(document: WorkSettingsDocument) -> bool:
    """새 설치의 빈 문서만 최초 가져오기 대상으로 구분한다."""
    # 표시 규칙 버전만 없던 초기 문서도 한 번의 파일 가져오기를 허용한다.
    normalized = {"mainListRulesVersion": 1, "classificationVersion": 4, **document.payload}
    if normalized["classificationVersion"] in (1, 2, 3):
        normalized["classificationVersion"] = 4
    return document.revision == 1 and normalized == empty_work_settings()


def source_documents(payload: dict[str, object]) -> list[dict[str, object]]:
    """JSON 소스 목록의 키를 검사하고 수정 가능한 사본을 반환한다."""
    raw = payload.get("sources")
    if not isinstance(raw, list):
        raise WorkSettingsError("invalid")
    result: list[dict[str, object]] = []
    for source in raw:
        if not isinstance(source, dict) or any(not isinstance(key, str) for key in source):
            raise WorkSettingsError("invalid")
        result.append({key: value for key, value in source.items()})
    return result
