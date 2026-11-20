"""현재 스냅샷에서 오늘 조회와 업무 검색에 필요한 본인 자료를 선별한다."""

import json
import re
from collections.abc import Callable, Iterable, Iterator, Mapping
from dataclasses import dataclass, replace
from datetime import date, datetime
from functools import lru_cache, partial

from src.application.date_inputs import parse_day
from src.application.errors import QueryReadError
from src.application.ports import SnapshotReader
from src.application.today_metadata import source_metadata, utc_string
from src.application.today_models import MainListCard, SourceSnapshot, TodayContext
from src.domain.display_columns import DisplayColumn, labelled_values
from src.domain.main_list import STATUS_GROUP_NAMES
from src.domain.models import CalendarEvent, Leave, SourceMapping, Task, WorkItem
from src.domain.policies import RelevancePolicy
from src.domain.task_display import TaskDisplayGroup, TaskDisplayPolicy, display_group
from src.domain.work_dates import SEOUL, canonical_identity, deadline_flags, meeting_on_day


def task_view(
    item: Task,
    source: SourceMapping,
    context: TodayContext,
    day: date,
    now: datetime,
) -> dict[str, object] | None:
    """완료 기준이 없으면 추측하지 않고 판정 불가 상태를 남긴다."""
    configured = context.completed_statuses.get(item.source_id)
    completed = configured is not None and item.status in configured
    display = context.qa_display_policy if source.section == "qa" else context.task_display_policy
    if source.source_id not in context.desktop_areas and not display.includes(
        item, completed=completed, day=day
    ):
        return None
    due_today, overdue = deadline_flags(item.due, day, now)
    tracks_status = "status" in source.properties or not source.flexible
    return {
        "id": item.id,
        "sourceId": item.source_id,
        "title": item.title,
        "status": item.status,
        **({"tracksStatus": False} if not tracks_status else {}),
        "section": source.section,
        "qaRound": item.qa_round,
        "url": item.url,
        "due": item.due.payload() if item.due else None,
        "dueToday": due_today,
        "overdue": overdue,
        "completionState": "completed"
        if completed
        else "not_completed"
        if configured is not None
        else "unknown"
        if tracks_status
        else "not_applicable",
    }


def calendar_view(item: CalendarEvent) -> dict[str, object]:
    """원본 분류를 유지하고 휴가 승인은 별도 판단임을 표시한다."""
    value: dict[str, object] = {
        "id": item.id,
        "sourceId": item.source_id,
        "title": item.title,
        "url": item.url,
        "period": item.period.payload() if item.period else None,
        "allDay": item.period is not None and item.period.kind == "date",
        "category": item.category,
        "eventType": item.event_type,
    }
    if item.cancelled:
        value["cancelled"] = True
    if item.event_type == "leave":
        value["approvalState"] = "not_evaluated"
    return value


def empty_groups() -> dict[str, list[dict[str, object]]]:
    """반환 목록의 이름을 한 곳에서 정의한다."""
    return {"tasks": [], "qa": [], "meetings": [], "leaves": [], "otherEvents": []}


def desktop_fields(
    columns: tuple[DisplayColumn, ...], values: Mapping[str, str]
) -> dict[str, object]:
    """임의 칼럼은 관리자 메뉴바 응답에만 사용한다."""
    fields: list[dict[str, object]] = []
    for column, (name, value) in zip(columns, labelled_values(columns, values), strict=True):
        field: dict[str, object] = {"label": name, "value": value}
        # 표시값에만 메타데이터를 붙여 숨긴 상태와 일반 조회에는 노출하지 않는다.
        if column.shows_status_badge:
            field["badge"] = True
        fields.append(field)
    return {"displayFields": fields}


def main_list_projection(source: SourceMapping, card: MainListCard | None) -> SourceMapping:
    """메인 카드 조회에서만 숨긴 조건·정렬 칼럼을 추가로 읽는다."""
    if card is None:
        return source
    fields = card.rules.fields
    visible = source.display_columns or ()
    hidden = tuple(
        column for column in source.storage_layout if column.id in fields and column not in visible
    )
    return replace(source, display_columns=(*visible, *hidden)) if hidden else source


def round_number(name: object) -> int:
    """차시 이름에서 첫 숫자를 읽고 숫자가 없으면 기존 정렬값 0을 사용한다."""
    match = re.search(r"\d+", name) if isinstance(name, str) else None
    return int(match.group()) if match else 0


def main_list_values(
    item: WorkItem, source: SourceMapping, row: Mapping[str, object], day: date, fields: set[str]
) -> dict[str, str]:
    """규칙에만 쓰는 숨긴 칼럼은 응답 행 밖에서 비교한다."""
    period = item.due if isinstance(item, Task) else item.period
    values = {
        column.id: item.display_values[column.value_key]
        for column in source.storage_layout
        if column.value_key in item.display_values
    }
    values.update(title=str(row["title"]), edited=item.source_edited_at)
    if period is not None:
        deadline = (period.end or period.start) if isinstance(item, Task) else period.start
        values.update(date=deadline, sourceDate=deadline)
    if isinstance(item, (CalendarEvent, Leave)):
        values["onDay"] = "예" if meeting_on_day(period, day) else "아니요"
        values["cancelled"] = (
            "예" if isinstance(item, CalendarEvent) and item.cancelled else "아니요"
        )
    if isinstance(item, Task):
        completed = row["completionState"] == "completed"
        group = TaskDisplayGroup.COMPLETED if completed else display_group(item.status)
        values.update(
            status=item.status or "",
            qaRound=item.qa_round or "",
            statusGroup=STATUS_GROUP_NAMES[group],
            overdue="예" if row["overdue"] else "아니요",
            dueToday="예" if row["dueToday"] else "아니요",
        )
        if "roundNumber" in fields:
            values["roundNumber"] = str(round_number(item.qa_round))
        # 완료 판정이 없는 소스는 미완료와 구별한다.
        if row["completionState"] in ("completed", "not_completed"):
            values["completed"] = "예" if completed else "아니요"
        if source.section == "qa":
            values["qaStatus"] = item.status or ""
            if completed:
                values["completedQaRound"] = item.qa_round or ""
                if "completedQaAge" in fields and item.source_edited_at:
                    edited = datetime.fromisoformat(item.source_edited_at)
                    if edited.tzinfo is not None:
                        values["completedQaAge"] = str((day - edited.astimezone(SEOUL).date()).days)
        elif completed and item.due is not None and "completedTaskAge" in fields:
            # 기간 기한은 마지막 날, 시각 기한은 서울 현지 날짜를 기준으로 센다.
            deadline = item.due.end or item.due.start
            deadline_day = (
                date.fromisoformat(deadline)
                if item.due.kind == "date"
                else datetime.fromisoformat(deadline).astimezone(SEOUL).date()
            )
            values["completedTaskAge"] = str((day - deadline_day).days)
    return values


type SortValues = dict[tuple[str, str], dict[str, str]]
type PreviewValues = dict[tuple[str, str], Mapping[str, str]]
type ScopedRow = tuple[str, WorkItem, dict[str, object]]


def scoped_rows(
    snapshot: SourceSnapshot,
    source: SourceMapping,
    context: TodayContext,
    policy: RelevancePolicy,
    day: date,
    now: datetime,
    task_filter: Callable[[Task], bool] | None = None,
) -> Iterator[ScopedRow]:
    """소스의 사람 범위와 일반 오늘 조회의 표시 규칙을 확인한다."""
    if not snapshot.mapping_matches:
        return
    # 같은 소스에서 반복되는 담당자·참석자 ID는 이 조회 동안만 정규화 결과를 재사용한다.
    normalize_id = lru_cache(maxsize=128)(canonical_identity)
    user_id = normalize_id(context.user_id)
    all_rows = not source.has_person_scope
    desktop = source.source_id in context.desktop_areas
    for item in snapshot.items:
        if isinstance(item, Task) and (
            all_rows or policy.includes(tuple(map(normalize_id, item.assignee_ids)), (), user_id)
        ):
            if task_filter is not None and not task_filter(item):
                continue
            selected = task_view(item, source, context, day, now)
            if selected is None:
                continue
            group = "qa" if source.section == "qa" else "tasks"
        elif (
            isinstance(item, CalendarEvent)
            and (
                all_rows
                or policy.includes((), tuple(map(normalize_id, item.participant_ids)), user_id)
            )
            and (desktop or (not item.cancelled and meeting_on_day(item.period, day)))
        ):
            group = {"meeting": "meetings", "leave": "leaves", "other": "otherEvents"}[
                item.event_type
            ]
            selected = calendar_view(item)
        elif (
            isinstance(item, Leave)
            and user_id in map(normalize_id, item.person_ids)
            and (desktop or meeting_on_day(item.period, day))
        ):
            group = "leaves"
            selected = {
                "id": item.id,
                "sourceId": item.source_id,
                "title": "휴가",
                "url": item.url,
                "period": item.period.payload(),
                "allDay": item.period.kind == "date",
                "approvalState": "approved" if item.approved else "not_evaluated",
            }
        else:
            continue
        yield group, item, selected


def task_sort_key(
    item: dict[str, object], *, display_policy: TaskDisplayPolicy
) -> tuple[int, int, bool, bool, str, str, str]:
    """상태·최근 QA 차시를 먼저 적용하고 같은 그룹은 기한·식별자로 정렬한다."""
    due = item["due"]
    deadline = str(due.get("end") or due.get("start")) if isinstance(due, dict) else "9999-12-31"
    return (
        display_policy.priority(
            item["status"] if isinstance(item["status"], str) else None,
            completed=item["completionState"] == "completed",
        ),
        -round_number(item.get("qaRound")),
        not bool(item["overdue"]),
        not bool(item["dueToday"]),
        deadline,
        str(item["id"]),
        str(item["sourceId"]),
    )


def event_sort_key(item: dict[str, object]) -> tuple[str, str, str]:
    """일정의 시작값과 식별자로 순서를 안정적으로 유지한다."""
    period = item["period"]
    start = str(period.get("start", "")) if isinstance(period, dict) else ""
    return start, str(item["id"]), str(item["sourceId"])


def main_card_rows(
    rows: Iterable[ScopedRow],
    source: SourceMapping,
    card: MainListCard,
    context: TodayContext,
    day: date,
    source_order: int,
    sort_values: SortValues,
) -> Iterator[ScopedRow]:
    """사람 범위 안의 행에만 카테고리 조건을 적용하고 카드 전체의 정렬값을 보관한다."""
    fields = card.rules.fields
    if not fields:
        yield from rows
        return
    positions: dict[str, int] = {}
    order = context.desktop_areas.get(source.source_id)
    if "sourceRowOrder" in fields and order in ("date", "title"):
        # 이전 표의 순번을 요청했을 때만 당시 동률 기준을 재현하고 원본 읽기 순서는 보존한다.
        rows = list(rows)
        if source.kind == "task":
            ordered = sorted(
                rows, key=lambda r: task_sort_key(r[2], display_policy=context.task_display_policy)
            )
        else:
            groups = {name: rank for rank, name in enumerate(empty_groups())}
            ordered = sorted(rows, key=lambda r: (groups[r[0]], event_sort_key(r[2])))
        if order == "title":
            ordered.sort(key=lambda r: str(r[2]["title"]).casefold())
        else:
            ordered.sort(key=lambda r: desktop_date_key(r[2]))
        positions = {item.id: index for index, (_, item, _) in enumerate(ordered, 1)}
    for group, item, row in rows:
        values = main_list_values(item, source, row, day, fields)
        values["sourceOrder"] = str(source_order)
        if item.id in positions:
            values["sourceRowOrder"] = str(positions[item.id])
        if card.rules.includes(values, card.kinds, day):
            if card.rules.sorts:
                sort_values[(item.source_id, item.id)] = values
            yield group, item, row


def desktop_card_previews(
    areas: dict[str, list[dict[str, object]]],
    context: TodayContext,
    sort_values: SortValues,
    preview_values: PreviewValues,
) -> tuple[dict[str, dict[str, object]], dict[str, dict[str, object]]]:
    """별도 정렬을 지정한 경우에만 스냅샷 행을 재배치한다."""
    ordering, cards = context.desktop_areas, context.main_cards
    if not ordering:
        return {}, {}
    result: dict[str, dict[str, object]] = {}
    categories: dict[str, list[dict[str, object]]] = {}
    category_rules = {card.key: card for card in cards.values()}
    # 미리보기를 자르기 전에 같은 카드의 소스를 모두 모아야 앞순위 행이 누락되지 않는다.
    area_count = len(areas.keys() - cards.keys()) + len(category_rules)
    limit = max(1, 500 // area_count)
    budget = 640_000 // area_count
    preview = partial(
        desktop_preview, limit=limit, budget=budget, context=context, preview_values=preview_values
    )
    for source_id, items in areas.items():
        order = ordering[source_id]
        if order == "title":
            items.sort(key=lambda item: str(item["title"]).casefold())
        elif order == "date":
            items.sort(key=desktop_date_key)
        if source_id in cards:
            categories.setdefault(cards[source_id].key, []).extend(items)
            continue
        result[source_id] = {
            "items": preview(items),
            "count": len(items),
            "calendar": bool(items and "period" in items[0]),
        }
    category_areas: dict[str, dict[str, object]] = {}
    for key, items in categories.items():
        card = category_rules[key]
        if card.rules.sorts:
            card.rules.order(
                items, lambda row: sort_values[(str(row["sourceId"]), str(row["id"]))], card.kinds
            )
        category_areas[key] = {"items": preview(items), "count": len(items)}
    return result, category_areas


def desktop_preview(
    items: list[dict[str, object]],
    *,
    limit: int,
    budget: int,
    context: TodayContext,
    preview_values: PreviewValues,
) -> list[dict[str, object]]:
    """긴 비고가 있어도 카드가 조회 한도 안에서 일부 행부터 표시되게 한다."""
    selected: list[dict[str, object]] = []
    used = 0
    for item in items[:limit]:
        # 앞순위 행에만 표시 칼럼을 붙인 뒤 최종 응답과 같은 JSON 크기를 잰다.
        source_id = str(item["sourceId"])
        values = preview_values.get((source_id, str(item["id"])))
        if values is not None:
            item.update(desktop_fields(context.display_columns[source_id], values))
        used += len(json.dumps(item, ensure_ascii=True)) + 2
        if selected and used > budget:
            break
        selected.append(item)
    return selected


def desktop_date_key(item: dict[str, object]) -> tuple[str, str]:
    """업무는 기한, 일정은 시작 시각 순서로 정렬한다."""
    value = item.get("due", item.get("period"))
    day = value.get("start") if isinstance(value, dict) else None
    if "due" in item and isinstance(value, dict):
        day = value.get("end") or day
    return str(day or "~"), str(item["title"]).casefold()


@dataclass(frozen=True)
class PersonalWorkRows:
    """응답을 자르기 전의 개인별 목록과 같은 읽기 시점의 수집 근거다."""

    metadata: dict[str, object]
    groups: dict[str, list[dict[str, object]]]
    desktop_areas: dict[str, dict[str, object]]
    category_areas: dict[str, dict[str, object]]


def checked_query_result(payload: dict[str, object]) -> dict[str, object]:
    """실제로 반환할 개인 조회 응답의 JSON 크기를 확인한다."""
    if len(json.dumps(payload, ensure_ascii=True)) > 1024 * 1024:
        raise QueryReadError()
    return payload


@dataclass(frozen=True)
class PersonalWorkSelection:
    """한 읽기 시점의 수집 근거와 아직 표시 목록에 담지 않은 소스별 행이다."""

    metadata: dict[str, object]
    batches: list[tuple[SourceMapping, Iterator[ScopedRow]]]
    max_rows: int
    sort_values: SortValues

    def rows(
        self,
        context: TodayContext,
        *,
        preview_values: PreviewValues | None = None,
    ) -> Iterator[tuple[str, str, dict[str, object]]]:
        """모든 행의 경고를 확인하고, 카드 미리보기는 정렬 후 표시값을 만들도록 원본을 남긴다."""
        for source, batch in self.batches:
            for group, item, row in batch:
                if row.get("completionState") == "unknown" and not self.metadata["warnings"]:
                    self.metadata["warnings"] = ["completion_status_unconfigured"]
                columns = context.display_columns.get(item.source_id)
                if columns is not None:
                    if preview_values is not None and source.source_id in context.desktop_areas:
                        preview_values[(item.source_id, item.id)] = item.display_values
                    else:
                        row.update(desktop_fields(columns, item.display_values))
                yield source.source_id, group, row


async def read_personal_sources(
    requested_date: str | None,
    reader: SnapshotReader,
    context: TodayContext,
    policy: RelevancePolicy,
    now: Callable[[], datetime],
    *,
    task_filter: Callable[[Task], bool] | None = None,
) -> PersonalWorkSelection:
    """조회는 저장 자료만 읽으며 수집·확인 커서·발송 상태를 변경하지 않는다."""
    requested_day = parse_day(requested_date) if requested_date is not None else None
    configured = context.sources
    snapshots = await reader.read_sources(
        tuple(
            main_list_projection(source, context.main_cards.get(source.source_id))
            for source in configured
        )
    )
    # 조회 도중 정상 수집이 끝날 수 있으므로 읽기 완료 뒤의 시점을 기준으로 삼는다.
    observed_now = now()
    if observed_now.tzinfo is None:
        raise QueryReadError()
    day = requested_day if requested_day is not None else observed_now.astimezone(SEOUL).date()
    by_id = {snapshot.source_id: snapshot for snapshot in snapshots}
    if len(by_id) != len(snapshots):
        raise QueryReadError()
    batches: list[tuple[SourceMapping, Iterator[ScopedRow]]] = []
    sources: list[dict[str, object]] = []
    successful: list[datetime] = []
    sort_values: SortValues = {}
    max_rows = 0
    for source_order, source in enumerate(configured, 1):
        snapshot = by_id.get(
            source.source_id, SourceSnapshot(source.source_id, source.kind, None, None, False, ())
        )
        if snapshot.kind != source.kind:
            raise QueryReadError()
        sources.append(source_metadata(snapshot, observed_now, context.max_age_seconds))
        if snapshot.last_success_at is not None and snapshot.mapping_matches:
            successful.append(snapshot.last_success_at)
        selected = scoped_rows(
            snapshot,
            source,
            context,
            policy,
            day,
            observed_now,
            task_filter,
        )
        if card := context.main_cards.get(source.source_id):
            selected = main_card_rows(
                selected, source, card, context, day, source_order, sort_values
            )
        batches.append((source, selected))
        if snapshot.mapping_matches:
            max_rows += len(snapshot.items)
    metadata: dict[str, object] = {
        "date": day.isoformat(),
        "timezone": "Asia/Seoul",
        "basis": "current_snapshot",
        "asOf": utc_string(observed_now),
        "lastSuccessfulSyncAt": utc_string(min(successful))
        if len(successful) == len(configured) and successful
        else None,
        "stale": not bool(configured) or any(bool(source["stale"]) for source in sources),
        "sources": sources,
        "warnings": [],
    }
    return PersonalWorkSelection(metadata, batches, max_rows, sort_values)


async def read_personal_work(
    requested_date: str | None,
    reader: SnapshotReader,
    context: TodayContext,
    policy: RelevancePolicy,
    now: Callable[[], datetime],
) -> PersonalWorkRows:
    """오늘 목록과 메인 카드는 소스별 표시 자료를 모두 모아 각 화면 순서로 정렬한다."""
    selection = await read_personal_sources(requested_date, reader, context, policy, now)
    groups = empty_groups()
    areas: dict[str, list[dict[str, object]]] = {key: [] for key in context.desktop_areas}
    preview_values: PreviewValues = {}
    for source_id, name, row in selection.rows(context, preview_values=preview_values):
        area = areas.get(source_id)
        (groups[name] if area is None else area).append(row)
    for name, values in groups.items():
        if name in {"tasks", "qa"}:
            values.sort(key=partial(task_sort_key, display_policy=context.task_display_policy))
        else:
            values.sort(key=event_sort_key)
    desktop_areas, category_areas = desktop_card_previews(
        areas, context, selection.sort_values, preview_values
    )
    return PersonalWorkRows(selection.metadata, groups, desktop_areas, category_areas)
