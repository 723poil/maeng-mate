"""카테고리 메인 카드에서 사용할 표시 조건과 정렬 순서를 정의한다."""

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import date, datetime, time
from decimal import Decimal, InvalidOperation

from src.domain.work_dates import SEOUL

# TaskDisplayGroup 순서에 대응하며 이전 규칙을 편집할 때도 같은 이름을 쓴다.
STATUS_GROUP_NAMES = ("진행", "미시작", "리뷰", "보류", "완료", "기타")
FIELD_KINDS = {
    "title": "text",
    "status": "text",
    "qaRound": "text",
    "parentLabel": "text",
    "parentOrder": "number",
    "date": "date",
    "sourceDate": "date",
    "edited": "date",
    "completed": "boolean",
    "cancelled": "boolean",
    "onDay": "boolean",
    "completedTaskAge": "number",
    "completedQaAge": "number",
    "qaStatus": "text",
    "completedQaRound": "text",
    "statusGroup": "text",
    "roundNumber": "number",
    "overdue": "boolean",
    "dueToday": "boolean",
    "sourceOrder": "number",
    "sourceRowOrder": "number",
}
RELATIVE_DAY_OPERATORS = ("past_days", "next_days", "since_days_ago")
_PRESENCE = ("empty", "not_empty")
_ORDERED = ("eq", "ne", "lt", "lte", "gt", "gte", *_PRESENCE)
_OPERATORS = {
    "text": ("eq", "exact", "ne", "contains", "not_contains", *_PRESENCE),
    "number": _ORDERED,
    "date": (*_ORDERED, "today", *RELATIVE_DAY_OPERATORS),
    "period": (*_ORDERED, "today", *RELATIVE_DAY_OPERATORS, "not_ended"),
    "time": _ORDERED,
    "boolean": ("eq", "ne", *_PRESENCE),
}
_BOOLEAN_VALUES = {"예": "1", "참": "1", "true": "1", "아니요": "0", "거짓": "0", "false": "0"}
type _Row = tuple[dict[str, object], Mapping[str, str]]


def operators_for(kind: str) -> tuple[str, ...]:
    """칼럼의 값 종류에 맞는 연산자를 편집기 표시 순서로 반환한다."""
    return _OPERATORS.get(kind, ())


def _read_value(raw: object, kind: str, *, date_only: bool = False) -> str | Decimal | None:
    """기록값을 같은 종류끼리 비교할 수 있게 바꾸고 누락·해석 실패를 구분 없이 처리한다."""
    value = raw.strip() if isinstance(raw, str) else ""
    if not value:
        return None
    if kind == "text":
        return value.casefold()
    if kind == "boolean":
        return _BOOLEAN_VALUES.get(value.casefold())
    try:
        if kind == "number":
            if "_" in value:
                return None
            number = Decimal(value)
            return number if number.is_finite() else None
        # 저장된 구간은 시작값으로 비교한다. 시간대가 없는 표시는 이미 서울 시각이다.
        start = value.partition(" ~ ")[0]
        if kind == "time":
            clock = time.fromisoformat(start)
            return clock.isoformat() if clock.tzinfo is None else None
        if kind == "date":
            stamp = datetime.fromisoformat(start)
            if stamp.tzinfo is not None:
                stamp = stamp.astimezone(SEOUL).replace(tzinfo=None)
            return stamp.date().isoformat() if date_only else stamp.isoformat()
    except (InvalidOperation, ValueError, OverflowError):
        # 이전 기록의 잘못된 값 하나 때문에 카드 전체의 조회가 실패하지 않도록 한다.
        return None
    return None


def _compare[T: (str, Decimal)](left: T, right: T, operator: str) -> bool:
    """같은 타입으로 해석한 두 값을 지정된 비교식으로 평가한다."""
    match operator:
        case "eq":
            return left == right
        case "ne":
            return left != right
        case "lt":
            return left < right
        case "lte":
            return left <= right
        case "gt":
            return left > right
        case "gte":
            return left >= right
        case _:
            return False


@dataclass(frozen=True)
class ListFilter:
    """한 칼럼의 값 또는 값의 유무를 검사할 조건이다."""

    field: str
    operator: str
    value: str = ""
    option_id: str | None = None

    def validate(self, kind: str) -> None:
        """선택한 연산자가 요구하는 종류와 형식의 입력값인지 확인한다."""
        if not isinstance(self.value, str):
            raise ValueError("비교할 값을 텍스트로 입력하세요.")
        if self.operator not in operators_for(kind):
            raise ValueError("칼럼과 조건을 다시 선택하세요.")
        if self.option_id is not None and (
            kind != "text"
            or self.operator not in {"eq", "exact", "ne"}
            or not self.option_id.strip()
            or self.option_id != self.option_id.strip()
            or any(ord(char) < 32 for char in self.option_id)
        ):
            raise ValueError("선택값 ID는 원본 선택 칼럼의 일치·불일치 조건에서 사용하세요.")
        if self.operator in (*_PRESENCE, "today", "not_ended"):
            if self.value:
                raise ValueError("이 조건은 입력값을 비워 두세요.")
            return
        if self.operator in RELATIVE_DAY_OPERATORS:
            days = self.value
            if not days.isascii() or not days.isdecimal() or not days.strip("0"):
                raise ValueError("기간은 1일 이상으로 입력하세요.")
            return
        if kind == "period":
            kind = "date"
        expected = _read_value(self.value, kind, date_only=True)
        if kind == "date" and (expected is None or self.value != expected):
            raise ValueError("날짜는 YYYY-MM-DD 형식으로 입력하세요.")
        if kind == "time" and expected != self.value + ":00":
            raise ValueError("시간은 HH:MM 형식으로 입력하세요.")
        if kind == "boolean" and self.value not in ("예", "아니요"):
            raise ValueError("‘예’ 또는 ‘아니요’를 선택하세요.")
        if expected is None:
            if kind == "number":
                raise ValueError("비교할 숫자를 입력하세요.")
            raise ValueError("비교할 값을 입력하세요.")

    def matches(self, raw: str, kind: str, day: date | None) -> bool:
        """누락값은 비어 있음 조건으로만 선택하고 날짜는 서울 기준일과 비교한다."""
        if self.operator == "exact":
            # QA 상태·차시처럼 원천 이름 자체를 지정한 조건은 공백과 대소문자도 구분한다.
            return raw == self.value
        actual = _read_value(raw, kind, date_only=True)
        if self.operator == "empty":
            return actual is None
        if actual is None:
            return False
        if self.operator == "not_empty":
            return True
        if self.operator in ("today", *RELATIVE_DAY_OPERATORS) and isinstance(actual, str):
            # 서울 기준일을 표현할 수 없어도 다른 종류의 조건은 그대로 평가한다.
            if day is None:
                return False
            distance = (date.fromisoformat(actual) - day).days
            if self.operator == "today":
                return distance == 0
            if self.operator == "since_days_ago":
                return distance >= -Decimal(self.value)
            # 오늘부터 양쪽 방향으로 각각 N개의 날짜를 센다.
            elapsed = -distance if self.operator == "past_days" else distance
            return 0 <= elapsed < Decimal(self.value)
        expected = _read_value(self.value, kind, date_only=True)
        if isinstance(actual, str) and isinstance(expected, str):
            if self.operator == "contains":
                return expected in actual
            if self.operator == "not_contains":
                return expected not in actual
            return _compare(actual, expected, self.operator)
        if isinstance(actual, Decimal) and isinstance(expected, Decimal):
            return _compare(actual, expected, self.operator)
        return False


@dataclass(frozen=True)
class ListSort:
    """카드 전체에 적용할 정렬 칼럼과 방향이다."""

    field: str
    descending: bool = False
    values: tuple[str, ...] = ()

    def validate(self, kind: str) -> None:
        """텍스트 우선순위는 비어 있지 않은 서로 다른 값을 순서대로 받는다."""
        if not operators_for(kind):
            raise ValueError("정렬할 칼럼을 다시 선택하세요.")
        if not isinstance(self.descending, bool):
            raise ValueError("오름차순이나 내림차순을 선택하세요.")
        if self.values and (kind != "text" or self.descending):
            raise ValueError(
                "값 순서를 직접 정하려면 텍스트 칼럼에서 ‘사용자 지정 순서’를 선택하세요."
            )
        seen: set[str] = set()
        for value in self.values:
            normalized = value.strip().casefold() if isinstance(value, str) else ""
            if not normalized:
                raise ValueError("빈칸에 정렬할 값을 입력하세요.")
            if normalized in seen:
                raise ValueError("같은 값이 두 번 들어갔습니다. 하나만 남겨 주세요.")
            seen.add(normalized)

    def sort_key(self, kind: str) -> Callable[[_Row], tuple[bool, int, str | Decimal]]:
        """카드와 소스에서 같은 타입 변환과 누락값 순서로 비교할 키를 만든다."""
        ranks: dict[str | Decimal, int] = {
            value.strip().casefold(): rank for rank, value in enumerate(self.values)
        }

        def sort_key(row: _Row) -> tuple[bool, int, str | Decimal]:
            """정렬 방향이 바뀌어도 빈 값이 있는 그룹은 마지막에 놓는다."""
            if self.field == "sourceDate":
                # 원본 순서는 날짜 전용 값과 UTC 시각을 저장된 ISO 값으로 비교한다.
                # 날짜가 없는 일정은 맨 앞, 기한이 없는 업무는 맨 뒤에 두던 순서를 보존한다.
                original = row[1].get(self.field, "9999-12-31" if "due" in row[0] else "")
                return self.descending, 0, original
            key = _read_value(row[1].get(self.field), kind)
            if key is None:
                return not self.descending, 0, ""
            return self.descending, ranks.get(key, len(ranks)), key

        return sort_key

    def apply(self, rows: list[_Row], kind: str) -> None:
        """빈 값과 직접 지정한 값의 순위를 먼저 구분하고 실제 값을 비교한다."""
        rows.sort(key=self.sort_key(kind), reverse=self.descending)


@dataclass(frozen=True)
class MainListRules:
    """사용자가 등록한 카테고리별 필터와 정렬이다."""

    match: str = "all"
    filters: tuple[ListFilter, ...] = ()
    sorts: tuple[ListSort, ...] = ()
    exclusions: tuple[ListFilter, ...] = ()

    @property
    def fields(self) -> set[str]:
        """수집과 조회가 같은 기준으로 숨긴 칼럼까지 준비하도록 참조를 모은다."""
        return {rule.field for rule in (*self.filters, *self.exclusions)} | {
            sort.field for sort in self.sorts
        }

    def validate(self, kinds: Mapping[str, str]) -> None:
        """입력 경계에서 참조 칼럼, 조건 연산자, 정렬 중복을 검증한다."""
        if self.match not in ("all", "any"):
            raise ValueError("‘모두 일치’ 또는 ‘하나라도 일치’를 선택하세요.")
        for condition in (*self.filters, *self.exclusions):
            condition.validate(kinds.get(condition.field, ""))
        sorted_fields: set[str] = set()
        for sort in self.sorts:
            sort.validate(kinds.get(sort.field, ""))
            if sort.field in sorted_fields:
                raise ValueError("같은 칼럼은 정렬 기준에 한 번만 추가하세요.")
            sorted_fields.add(sort.field)

    def includes(
        self, values: Mapping[str, str], kinds: Mapping[str, str], day: date | None
    ) -> bool:
        """제외 조건 중 하나라도 맞으면 숨기고 나머지 행에 포함 조건을 적용한다."""
        return self.accepts(
            lambda condition: condition.matches(
                values.get(condition.field, ""), kinds[condition.field], day
            )
        )

    def accepts(self, matches: Callable[[ListFilter], bool]) -> bool:
        """같은 포함·제외 결합식을 저장값과 원본 속성 비교에 함께 쓴다."""
        if any(matches(condition) for condition in self.exclusions):
            return False
        return not self.filters or (all if self.match == "all" else any)(
            matches(condition) for condition in self.filters
        )

    def order(
        self,
        items: list[dict[str, object]],
        values: Callable[[dict[str, object]], Mapping[str, str]],
        kinds: Mapping[str, str],
    ) -> None:
        """빈 값은 방향과 무관하게 뒤에 놓고, 동률이면 기존 순서를 보존한다."""
        if not self.sorts:
            return
        rows = [(item, values(item)) for item in items]
        # 낮은 우선순위부터 안정 정렬해야 앞쪽 정렬 규칙의 동률에서만 다음 규칙이 쓰인다.
        for sort in reversed(self.sorts):
            sort.apply(rows, kinds[sort.field])
        items[:] = [item for item, _ in rows]
