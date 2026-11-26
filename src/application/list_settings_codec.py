"""메인 목록의 필터와 정렬을 읽고 저장 형식으로 변환한다."""

from src.application.settings_validation import nonblank
from src.domain.category_configuration import CategoryColumn
from src.domain.main_list import FIELD_KINDS, ListFilter, ListSort, MainListRules


def parse_list_filters(raw: object, *, source_options: bool = False) -> tuple[ListFilter, ...]:
    """같은 형식으로 저장한 포함·제외 조건의 입력을 검사한다."""
    if not isinstance(raw, list):
        raise ValueError
    conditions: list[ListFilter] = []
    optional = {"optionId"} if source_options else set()
    for item in raw:
        if (
            not isinstance(item, dict)
            or set(item) - optional != {"field", "operator", "value"}
            or not isinstance(item["value"], str)
        ):
            raise ValueError
        conditions.append(
            ListFilter(
                nonblank(item["field"]),
                nonblank(item["operator"]),
                item["value"],
                nonblank(item["optionId"]) if "optionId" in item else None,
            )
        )
    return tuple(conditions)


def parse_main_list(raw: object, columns: tuple[CategoryColumn, ...]) -> MainListRules:
    """목록 규칙의 JSON 구조와 카테고리 안에서 사용할 수 있는 조건을 검증한다."""
    # 이전 체크박스의 해석은 설정 이전에서만 처리한다.
    if not isinstance(raw, dict) or set(raw) - {"useDefaultVisibility", "exclusions"} != {
        "match",
        "filters",
        "sorts",
    }:
        raise ValueError
    match, sorts = raw["match"], raw["sorts"]
    if (
        not isinstance(match, str)
        or ("useDefaultVisibility" in raw and type(raw["useDefaultVisibility"]) is not bool)
        or not isinstance(sorts, list)
    ):
        raise ValueError
    conditions = parse_list_filters(raw["filters"])
    exclusions = parse_list_filters(raw.get("exclusions", []))
    order = []
    for item in sorts:
        if (
            not isinstance(item, dict)
            or set(item) - {"values"} != {"field", "descending"}
            or type(item["descending"]) is not bool
        ):
            raise ValueError
        values = item.get("values", [])
        if not isinstance(values, list):
            raise ValueError
        order.append(
            ListSort(
                nonblank(item["field"]), item["descending"], tuple(nonblank(v) for v in values)
            )
        )
    rules = MainListRules(match, conditions, tuple(order), exclusions)
    # 표시를 끈 칼럼도 원본 값은 비교할 수 있으므로 등록된 칼럼 전체를 확인한다.
    rules.validate({**FIELD_KINDS, **{column.id: column.kind for column in columns}})
    return rules


def filter_conditions_payload(rules: MainListRules) -> dict[str, object]:
    """목록과 알림의 포함·제외 조건을 같은 형식으로 기록한다."""

    def condition(item: ListFilter) -> dict[str, str]:
        """원본 옵션 ID가 있는 조건만 이름 변경을 추적하도록 저장한다."""
        return {
            "field": item.field,
            "operator": item.operator,
            "value": item.value,
            **({"optionId": item.option_id} if item.option_id is not None else {}),
        }

    payload: dict[str, object] = {
        "match": rules.match,
        "filters": [condition(item) for item in rules.filters],
    }
    if rules.exclusions:
        payload["exclusions"] = [condition(item) for item in rules.exclusions]
    return payload


def main_list_payload(rules: MainListRules) -> dict[str, object]:
    """표시 조건에 목록 전용 정렬 우선순위를 더한다."""
    return {
        **filter_conditions_payload(rules),
        "sorts": [
            {
                "field": item.field,
                "descending": item.descending,
                **({"values": list(item.values)} if item.values else {}),
            }
            for item in rules.sorts
        ],
    }
