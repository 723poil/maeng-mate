"""이미 연결된 메인 카드의 예전 표시 방식을 편집 가능한 규칙으로 한 번 옮긴다."""

from dataclasses import replace
from uuid import UUID

from src.application.work_settings import (
    category_documents,
    main_list_payload,
    parse_settings,
    source_documents,
)
from src.domain.main_list import STATUS_GROUP_NAMES, ListFilter, ListSort, MainListRules
from src.domain.models import SourceMapping
from src.domain.task_display import QATaskDisplayPolicy, StatusTaskDisplayPolicy


def _visibility_opt_out(original: dict[str, object]) -> set[str]:
    """앞선 카테고리 이전에서 사라질 수 있는 옛 표시 스위치를 원문에서 읽는다."""
    categories = original.get("categories")
    if not isinstance(categories, list):
        categories = [source.get("category") for source in source_documents(original)]
    disabled: set[str] = set()
    for raw in categories:
        if not isinstance(raw, dict) or not isinstance(config := raw.get("configuration"), dict):
            continue
        rules = config.get("mainList")
        if isinstance(rules, dict) and rules.get("useDefaultVisibility") is False:
            disabled.add(str(UUID(str(raw["id"]).strip())))
    return disabled


def _legacy_exclusions(
    sources: list[SourceMapping], qa: QATaskDisplayPolicy, default_visibility: bool
) -> tuple[ListFilter, ...]:
    """소스 종류에 해당하는 항목만 비교할 수 있는 제외 조건을 만든다."""
    exclusions: list[ListFilter] = []
    if default_visibility:
        sections = {source.section for source in sources if source.kind == "task"}
        if "tasks" in sections:
            days = StatusTaskDisplayPolicy().completed_retention_days
            exclusions.append(ListFilter("completedTaskAge", "gt", str(days)))
        if "qa" in sections:
            exclusions.append(ListFilter("completedQaAge", "gt", str(qa.completed_retention_days)))
            exclusions.extend(
                ListFilter("qaStatus", "exact", value) for value in qa.hidden_statuses
            )
            exclusions.extend(
                ListFilter("completedQaRound", "exact", value)
                for value in qa.hidden_completed_rounds
            )
        if any(source.kind in ("meeting", "leave") for source in sources):
            exclusions.append(ListFilter("onDay", "eq", "아니요"))
    # 옛 조회는 기본 표시를 껐을 때도 취소 일정을 제외했다.
    if any(source.kind == "meeting" for source in sources):
        exclusions.append(ListFilter("cancelled", "eq", "예"))
    return tuple(exclusions)


def _legacy_sorts(members: list[tuple[SourceMapping, str]]) -> tuple[ListSort, ...]:
    """표 순서와 소스별 정렬을 보존하고 기본 정렬이었던 표의 기준만 펼친다."""
    sorts: list[ListSort] = []
    if len(members) > 1:
        sorts.append(ListSort("sourceOrder"))
    default = [source for source, order in members if order == "default"]
    if len(default) != len(members):
        sorts.append(ListSort("sourceRowOrder"))
    tasks = [source for source in default if source.kind == "task"]
    if tasks:
        sorts.append(ListSort("statusGroup", values=STATUS_GROUP_NAMES))
        if any(
            source.section == "qa" or source.round_title_property is not None for source in tasks
        ):
            sorts.append(ListSort("roundNumber", descending=True))
        sorts.extend((ListSort("overdue", descending=True), ListSort("dueToday", descending=True)))
    if default:
        sorts.append(ListSort("sourceDate"))
    return tuple(sorts)


def upgrade_main_list_rules(
    payload: dict[str, object], original: dict[str, object]
) -> dict[str, object] | None:
    """현재 연결된 카테고리만 이전하고 빈 문서에도 완료 버전을 남긴다."""
    if payload.get("mainListRulesVersion") == 1:
        return None
    settings = parse_settings(payload)
    members: dict[str, list[tuple[SourceMapping, str]]] = {}
    for source, style in zip(settings.sources, settings.display, strict=True):
        if style.category is not None:
            members.setdefault(style.category.id, []).append((source, style.sort))
    upgraded = {**payload, "mainListRulesVersion": 1}
    if not members:
        return upgraded
    disabled = _visibility_opt_out(original)
    raw_categories = payload.get("categories")
    if not isinstance(raw_categories, list):
        raw_categories = category_documents(settings.categories)
    categories = []
    for raw, category in zip(raw_categories, settings.categories, strict=True):
        config = category.configuration
        if category.id not in members or config is None:
            categories.append(raw)
            continue
        # 모두/하나 이상 일치와 사용자 정렬을 그대로 두고 옛 숨김만 개별 제외 조건으로 합친다.
        sources = [source for source, _ in members[category.id]]
        current = config.main_list or MainListRules()
        exclusions = _legacy_exclusions(
            sources, settings.qa_display_policy, category.id not in disabled
        )
        rules = replace(
            current,
            exclusions=tuple(dict.fromkeys((*current.exclusions, *exclusions))),
            sorts=current.sorts or _legacy_sorts(members[category.id]),
        )
        categories.append(
            {**raw, "configuration": {**raw["configuration"], "mainList": main_list_payload(rules)}}
        )
    return {**upgraded, "categories": categories}
