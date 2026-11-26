"""Notion 속성과 시간대를 내부 업무 값으로 변환한다."""

import re
from datetime import UTC, date, datetime
from urllib.parse import unquote, urlsplit
from zoneinfo import ZoneInfo

from src.application.category_column_types import scope_property_kinds
from src.domain.display_columns import DISPLAY_VALUE_LIMIT, TITLE_COLUMN_TYPES
from src.domain.event_classification import classify_event
from src.domain.main_list import ListFilter
from src.domain.models import CalendarEvent, Leave, Period, SourceMapping, Task, WorkItem
from src.domain.notification_timing import calendar_alert_expiry
from src.domain.raw_fields import checked_raw_field
from src.domain.source_alerts import SourceAlertRule, alert_value_key
from src.domain.source_scope import SourceScope, field_parts
from src.domain.work_dates import canonical_identity, seoul_clock, seoul_datetime
from src.infrastructure.notion.errors import SourceError
from src.infrastructure.notion.values import array, record, text


class PageProperties:
    """한 행의 원본 속성을 디코딩한 ID로 찾아 상세 조회와 정규화에서 공유한다."""

    def __init__(self, properties: dict[str, object]) -> None:
        """같은 ID를 가진 속성은 모호함을 표시해 그 칼럼을 사용할 때 거부한다."""
        self._by_id: dict[str, dict[str, object] | None] = {}
        for raw in properties.values():
            prop = record(raw)
            identifier = prop.get("id")
            if isinstance(identifier, str):
                key = unquote(identifier)
                self._by_id[key] = None if key in self._by_id else prop

    def by_id(self, identifier: str) -> dict[str, object]:
        """표시 이름과 인코딩 표기에 관계없이 단 하나의 원본 속성만 선택한다."""
        prop = self._by_id.get(unquote(identifier))
        if prop is None:
            raise SourceError("mapping_invalid")
        return prop


def instant(value: str, zone_name: str | None = None) -> str:
    """명시적 offset 또는 IANA 시간대로 UTC를 구하고 DST 모호성은 거부한다."""
    try:
        if "T" not in value:
            raise ValueError
        local = datetime.fromisoformat(value)
        if local.tzinfo is None:
            if zone_name is None:
                raise ValueError
            zone = ZoneInfo(zone_name)
            # 존재하지 않는 시각과 두 번 발생하는 시각을 자동 보정하지 않는다.
            candidates = {
                local.replace(tzinfo=zone, fold=fold).astimezone(UTC)
                for fold in (0, 1)
                if local.replace(tzinfo=zone, fold=fold)
                .astimezone(UTC)
                .astimezone(zone)
                .replace(tzinfo=None)
                == local
            }
            if len(candidates) != 1:
                raise ValueError
            resolved = next(iter(candidates))
        else:
            resolved = local.astimezone(UTC)
            if (
                zone_name
                and local.utcoffset() != resolved.astimezone(ZoneInfo(zone_name)).utcoffset()
            ):
                raise ValueError
        return resolved.isoformat(timespec="milliseconds").replace("+00:00", "Z")
    except (ValueError, KeyError, OverflowError):
        raise SourceError("mapping_invalid") from None


def read_period(prop: dict[str, object]) -> Period | None:
    """날짜 전용 값과 시점을 구분하고 역전·혼합 구간을 거부한다."""
    if prop.get("type") != "date" or "date" not in prop:
        raise SourceError("mapping_invalid")
    if prop.get("date") is None:
        return None
    raw = record(prop["date"])
    if "start" not in raw or "end" not in raw:
        raise SourceError("mapping_invalid")
    start, end = text(raw.get("start")), raw.get("end")
    zone = raw.get("time_zone")
    if zone is not None and not isinstance(zone, str):
        raise SourceError("mapping_invalid")
    is_date = re.fullmatch(r"\d{4}-\d{2}-\d{2}", start) is not None
    try:
        if is_date:
            if zone is not None:
                raise ValueError
            normalized_start = date.fromisoformat(start).isoformat()
            normalized_end = None if end is None else date.fromisoformat(text(end)).isoformat()
        else:
            normalized_start = instant(start, zone)
            normalized_end = None if end is None else instant(text(end), zone)
        if normalized_end is not None and normalized_end < normalized_start:
            raise ValueError
        return Period("date" if is_date else "instant", normalized_start, normalized_end)
    except ValueError:
        raise SourceError("mapping_invalid") from None


def read_people(prop: dict[str, object]) -> tuple[str, ...]:
    """사용자 순서와 중복으로 가짜 변경이 발생하지 않도록 정규화한다."""
    if prop.get("type") != "people":
        raise SourceError("mapping_invalid")
    return tuple(sorted({text(record(person).get("id")) for person in array(prop.get("people"))}))


def read_status(prop: dict[str, object]) -> str | None:
    """상태와 선택 속성을 지원하며 빈 상태도 보존한다."""
    kind = prop.get("type")
    if kind not in ("status", "select") or str(kind) not in prop:
        raise SourceError("mapping_invalid")
    value = prop.get(str(kind))
    return None if value is None else text(record(value).get("name"))


def read_checkbox(prop: dict[str, object]) -> bool:
    """취소 필드의 누락과 잘못된 값을 미취소로 처리하지 않는다."""
    if (
        prop.get("type") != "checkbox"
        or "checkbox" not in prop
        or type(prop["checkbox"]) is not bool
    ):
        raise SourceError("mapping_invalid")
    return prop["checkbox"]


def local_date(value: str) -> str:
    """시각은 화면과 같은 서울 시간대로 표시한다."""
    if "T" not in value:
        return value
    local = seoul_datetime(datetime.fromisoformat(value))
    return local.strftime("%Y-%m-%d %H:%M") if local else value


def column_text(prop: dict[str, object], kind: str) -> str:
    """원본 타입을 확인하고 이름이 없는 사람 ID는 화면에 노출하지 않는다."""
    if kind == "status_or_select":
        return read_status(prop) or ""
    if prop.get("type") != kind or kind not in prop:
        raise SourceError("mapping_invalid")
    raw = prop[kind]
    if raw is None:
        return ""
    if kind in {"title", "rich_text"}:
        return "".join(text(record(part).get("plain_text")) for part in array(raw))
    if kind in {"status", "select"}:
        return read_status(prop) or ""
    if kind in {"people", "multi_select"}:
        names = (record(item).get("name") for item in array(raw))
        return ", ".join(name for name in names if isinstance(name, str) and name)
    if kind == "checkbox":
        return "예" if read_checkbox(prop) else "아니요"
    if kind == "date":
        period = read_period(prop)
        if period is None:
            return ""
        return local_date(period.start) + (" ~ " + local_date(period.end) if period.end else "")
    if kind == "number":
        if type(raw) not in (int, float):
            raise SourceError("mapping_invalid")
        return str(raw)
    if kind in {"created_time", "last_edited_time"}:
        return local_date(instant(text(raw)))
    return text(raw)


def raw_column(prop: dict[str, object], kind: str) -> dict[str, object]:
    """이미 수집한 칼럼만 표시 생략·공백 축약·분류 변환 전에 보존한다."""
    value: object
    if kind in {"status", "select", "status_or_select"}:
        value = read_status(prop)
    elif kind == "people":
        # 사람 전체 객체의 이메일·사진 등은 원형 칼럼에도 저장하지 않는다.
        value = list(read_people(prop))
    elif kind == "multi_select":
        value = [text(record(item).get("name")) for item in array(prop.get(kind))]
    elif kind == "date":
        period = read_period(prop)
        value = period.payload() if period else None
    elif kind == "checkbox":
        value = read_checkbox(prop)
    elif kind == "number":
        value = prop.get(kind)
    elif kind in {"created_time", "last_edited_time"}:
        value = instant(text(prop[kind])) if prop.get(kind) is not None else None
    else:
        value = column_text(prop, kind) if prop.get(kind) is not None else None
    try:
        return checked_raw_field({"type": kind, "value": value})
    except ValueError:
        raise SourceError("mapping_invalid") from None


def alert_comparison(prop: dict[str, object], rule: SourceAlertRule, visible: str) -> str:
    """비교값에는 화면에서 생략한 초와 사람 식별자를 보존한다."""
    kind = rule.column_kind
    if rule.time_only:
        return clock_text(column_period(prop, kind))
    if kind == "people" and rule.target is None:
        people = set(map(canonical_identity, read_people(prop)))
        return ",".join(sorted(people))
    if kind == "date":
        period = read_period(prop)
        if period is None:
            return ""
        return period.start + (" ~ " + period.end if period.end else "")
    if kind in {"created_time", "last_edited_time"} and prop[kind] is not None:
        return instant(text(prop[kind]))
    if rule.target is None and kind in {"title", "rich_text"}:
        return column_text(prop, kind)
    return visible


def column_period(prop: dict[str, object], kind: str) -> Period | None:
    """일반 날짜와 생성·수정 시각을 같은 기간 모델로 읽는다."""
    if kind == "date":
        return read_period(prop)
    if kind in {"created_time", "last_edited_time"} and prop[kind] is not None:
        return Period("instant", instant(text(prop[kind])), None)
    return None


def clock_text(period: Period | None) -> str:
    """날짜만 있는 값은 비우고 시각은 서울 시간의 시·분으로 표시한다."""
    if period is None or period.kind != "instant":
        return ""
    return " ~ ".join(
        seoul_clock(datetime.fromisoformat(value))
        for value in (period.start, period.end)
        if value is not None
    )


def matches_source_scope(
    scope: SourceScope, props: PageProperties, observed: datetime, basis: datetime
) -> bool:
    """표시명이나 내부 분류 대신 현재 행의 원본 속성 전체 값을 비교한다."""

    def matches(condition: ListFilter) -> bool:
        """종일 범위와 정밀 시각의 종료 여부는 원본 종료값과 관측 시각으로 확인한다."""
        kind, identifier = field_parts(condition.field)
        prop = props.by_id(identifier)
        origin_kind = text(prop.get("type"))
        value_kind = "date" if kind == "period" else kind
        if origin_kind not in scope_property_kinds(condition):
            raise SourceError("mapping_invalid")
        if condition.option_id is not None:
            # 표시 이름이 바뀌거나 같은 이름이 재사용돼도 선택한 원본 옵션만 일치한다.
            if origin_kind not in prop:
                raise SourceError("mapping_invalid")
            selection = prop[origin_kind]
            if selection is None:
                return False
            actual = text(record(selection).get("id"))
            if not actual.strip():
                raise SourceError("mapping_invalid")
            return (
                actual != condition.option_id
                if condition.operator == "ne"
                else actual == condition.option_id
            )
        if condition.operator == "not_ended":
            return calendar_alert_expiry(column_period(prop, origin_kind), observed) is not None
        local = seoul_datetime(basis)
        return condition.matches(
            column_text(prop, origin_kind), value_kind, local.date() if local else None
        )

    return scope.conditions.accepts(matches)


def normalize_page(
    raw: object,
    source: SourceMapping,
    observed_at: str,
    *,
    properties: PageProperties | None = None,
    user_id: str | None = None,
) -> WorkItem:
    """완전히 수집한 필드만 순수 업무 모델로 변환한다."""
    page = record(raw)
    if page.get("object") != "page" or urlsplit(text(page.get("url"))).scheme != "https":
        raise SourceError("invalid_response")
    base = {
        "id": text(page.get("id")),
        "source_id": source.source_id,
        "url": text(page.get("url")),
        "source_edited_at": instant(text(page.get("last_edited_time"))),
        "observed_at": instant(observed_at),
    }

    props = properties if properties is not None else PageProperties(record(page.get("properties")))
    raw_fields = (
        {
            column.value_key: raw_column(props.by_id(column.property_id), column.kind)
            for column in source.storage_layout
        }
        if source.kind == "task"
        else {}
    )

    def get(name: str) -> dict[str, object]:
        """회사별 매핑을 통해 원천 속성을 선택한다."""
        return props.by_id(source.properties[name])

    originals = {
        identifier: column_text(props.by_id(identifier), kind)
        for identifier, kind in source.collected_columns().items()
    }
    values = {identifier: " ".join(value.split()) for identifier, value in originals.items()}
    display = {
        column.value_key: (
            clock_text(column_period(props.by_id(column.property_id), column.kind))
            if column.time_only
            else values[column.property_id]
            if column.full_text
            else values[column.property_id][:DISPLAY_VALUE_LIMIT]
        )
        for column in source.storage_layout
    }
    classification = None
    if column := source.classification_column:
        # 분류되지 않은 값은 원문을 유지하고 알림의 원본 비교에도 원래 값을 쓴다.
        original = column_text(props.by_id(column.property_id), column.kind)
        classification = source.classification_values.get(
            original, source.classification_default or original
        )
        # 조건은 표시용 생략이나 분류 결과 대입 전의 원문을 비교한다.
        classification = next(
            (rule.result for rule in source.classification_rules if rule.matches(originals)),
            classification,
        )
        display[column.value_key] = (
            classification if column.full_text else classification[:DISPLAY_VALUE_LIMIT]
        )
    dates = {
        column.value_key: value
        for column in source.stored_columns
        if not column.time_only
        and column.kind in {"date", "created_time", "last_edited_time"}
        and (value := column_period(props.by_id(column.property_id), column.kind)) is not None
    }
    alerts = {}
    due_at = {}
    for rule in source.alert_rules:
        scope = rule.scope_for(source.source_id)
        if not rule.enabled or (rule.source_scopes is not None and scope is None):
            continue
        prop = props.by_id(rule.column_id)
        if rule.member is not None:
            member = user_id if rule.member == "self" else rule.member
            if member:
                present = canonical_identity(member) in map(canonical_identity, read_people(prop))
                alerts[rule.id] = alert_value_key("present" if present else "absent")
        elif rule.schedule is not None:
            # 시간 칼럼도 표시용 HH:MM 대신 원본 날짜와 시간대를 함께 사용한다.
            period = column_period(prop, rule.column_kind)
            if period is None or (rule.time_only and period.kind == "date"):
                continue
            due = rule.schedule.due_at(period.start)
            if due is None:
                continue
            due_at[rule.id] = due
            alerts[rule.id] = alert_value_key(due.isoformat())
        else:
            alerts[rule.id] = alert_value_key(
                alert_comparison(prop, rule, values[rule.column_id]), rule.comparison_kind
            )

        # 미대상 행에서 비교값 자체를 없애면 저장·발송 경로 모두 새 예약을 차단한다.
        if scope is not None and rule.id in alerts:
            observed = datetime.fromisoformat(base["observed_at"])
            if not matches_source_scope(scope, props, observed, due_at.get(rule.id, observed)):
                del alerts[rule.id]
                due_at.pop(rule.id, None)

    if source.kind == "leave":
        period = read_period(get("period"))
        if period is None:
            raise SourceError("mapping_invalid")
        approval = read_status(get("approval"))
        return Leave(
            **base,
            display_values=display,
            raw_fields=raw_fields,
            alert_values=alerts,
            alert_due_at=due_at,
            calendar_periods=dates,
            person_ids=read_people(get("people")),
            period=period,
            approval=approval,
            approved=approval in source.approved_values,
        )
    title_prop = get("title")
    title_kind = title_prop.get("type")
    if not isinstance(title_kind, str) or title_kind not in TITLE_COLUMN_TYPES:
        raise SourceError("mapping_invalid")
    title = column_text(title_prop, title_kind)
    if source.kind == "meeting":
        category = read_status(get("category")) if "category" in source.properties else None
        return CalendarEvent(
            **base,
            display_values=display,
            raw_fields=raw_fields,
            alert_values=alerts,
            alert_due_at=due_at,
            calendar_periods=dates,
            title=title,
            participant_ids=read_people(get("participants"))
            if "participants" in source.properties
            else (),
            period=read_period(get("period")),
            category=classification if classification is not None else category,
            event_type=classify_event(category, source.event_types)
            if "category" in source.properties
            else source.default_event_type,
            cancelled=read_checkbox(get("cancelled"))
            if "cancelled" in source.properties
            else False,
        )
    return Task(
        **base,
        display_values=display,
        raw_fields=raw_fields,
        alert_values=alerts,
        alert_due_at=due_at,
        calendar_periods=dates,
        title=title,
        assignee_ids=read_people(get("assignees")) if "assignees" in source.properties else (),
        status=read_status(get("status")) if "status" in source.properties else None,
        due=read_period(get("dueDate")) if "dueDate" in source.properties else None,
    )
