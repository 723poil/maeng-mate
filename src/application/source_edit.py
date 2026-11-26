"""소스 편집값에서 기존 표의 매핑과 버전을 보존하는 초안을 만든다."""

from dataclasses import dataclass
from urllib.parse import unquote

from src.application.category_settings_codec import column_bindings_payload
from src.application.notion_page_links import PageLinksError, page_url
from src.application.source_settings import SourceDraft, SourceSettingsError
from src.domain.category_configuration import ColumnBinding
from src.domain.models import SourceKind
from src.domain.source_roles import ColumnRole, roles_payload
from src.domain.work_dates import canonical_identity

EVENT_CHOICES = ("other", "meeting", "leave")


def uses_saved_layout(
    original: dict[str, object], schema_id: str | None, has_profiles: bool, rounds: bool
) -> bool:
    """같은 표와 같은 차시 탐색 방식에서만 저장한 매핑을 재사용한다."""
    saved = original.get("dataSourceId")
    if not isinstance(saved, str) or rounds != (
        "parentTitleProperty" in original or "roundTitleProperty" in original
    ):
        return False
    return (
        not has_profiles
        if schema_id is None
        else canonical_identity(schema_id) == canonical_identity(saved)
    )


def matches_property(candidate: str, previous: str, rounds: bool) -> bool:
    """하위 표는 이름을 그대로 비교하고 일반 표는 속성 ID의 인코딩 차이를 허용한다."""
    return candidate == previous if rounds else unquote(candidate) == unquote(previous)


def selected_properties(selected: dict[str, str], original: object, rounds: bool) -> dict[str, str]:
    """같은 속성을 다시 선택하면 저장된 ID의 원래 표기를 보존한다."""
    if not isinstance(original, dict):
        return selected
    return {
        key: previous
        if isinstance(previous := original.get(key), str)
        and matches_property(candidate, previous, rounds)
        else candidate
        for key, candidate in selected.items()
    }


@dataclass(frozen=True, repr=False)
class TableEdit:
    """조회한 표에서 선택한 원본 속성과 역할을 담는다."""

    id: str
    values: dict[str, str]
    labels: dict[str, str]
    roles: tuple[ColumnRole, ...]
    parent_title: str | None
    choices: dict[str, int]


@dataclass(frozen=True, repr=False)
class SourceEdit:
    """화면 컨트롤에서 읽은 소스 하나의 편집값이다."""

    category_id: str | None
    reference: str
    lookup_reference: str
    has_profiles: bool
    kind: SourceKind
    section: str
    table: TableEdit | None
    parent_order: tuple[str, str | None] | None
    parent_label: bool
    classification: dict[str, object]
    links: dict[str, ColumnBinding]
    name: str
    url: str
    source_id: str
    sort: str
    descending: bool


def source_draft(settings: SourceDraft, index: int, edit: SourceEdit | None) -> SourceDraft:
    """선택한 소스만 교체하거나 삭제하고 조회 시점의 버전을 유지한다."""
    sources = list(settings.sources)
    if edit is None:
        sources.pop(index)
        return SourceDraft(settings.revision, tuple(sources))
    original = sources[index] if index < len(sources) else {}
    if edit.category_id is None:
        raise SourceSettingsError("수집 소스의 카테고리를 선택하세요.")
    table = edit.table
    same = uses_saved_layout(
        original, table.id if table else None, edit.has_profiles, edit.parent_order is not None
    )
    reference = edit.reference
    if (table is not None and reference != edit.lookup_reference) or (
        table is None and reference != original.get("dataSourceId", "")
    ):
        raise SourceSettingsError("바뀐 주소로 칼럼을 조회하고 수집할 표를 선택하세요.")
    kind, section = edit.kind, edit.section
    if table is None:
        if not same or original.get("kind") != kind:
            raise SourceSettingsError("Notion 칼럼을 조회하고 수집할 표를 선택하세요.")
        value = dict(original)
    else:
        value = table_document(table, kind, section, original if same else {})
    if section == "qa":
        value["section"] = section
    else:
        value.pop("section", None)
    if edit.parent_order is not None:
        if "roundTitleProperty" in value:
            value["parentTitleProperty"] = value.pop("roundTitleProperty")
        mode, number = edit.parent_order
        value["parentOrder"] = mode
        value["parentLabel"] = edit.parent_label
        value.pop("parentNumberProperty", None)
        if number is not None:
            value["parentNumberProperty"] = number
    value.pop("classification", None)
    if edit.classification:
        value["classification"] = dict(edit.classification)
    value.pop("columnMappings", None)
    if edit.links:
        value["columnMappings"] = column_bindings_payload(edit.links)
    name = edit.name
    if len(name) > 120:
        raise SourceSettingsError("이름은 120자 이내로 입력하세요.")
    value.pop("name", None)
    if name:
        value["name"] = name
    value["categoryId"] = edit.category_id
    try:
        url = page_url(edit.url)
    except PageLinksError:
        raise SourceSettingsError("열려는 Notion 페이지의 주소를 입력하세요.") from None
    value.pop("pageUrl", None)
    if url:
        value["pageUrl"] = url
    # 편집은 기존 논리 ID를 유지하고 같은 원본을 새로 등록하면 다른 ID를 부여한다.
    value["sourceId"] = edit.source_id
    value.pop("sortDirection", None)
    value.pop("sort", None)
    sort = edit.sort
    if sort != "default":
        value["sort"] = sort
        if edit.descending:
            value["sortDirection"] = "desc"
    if index == len(sources):
        sources.append(value)
    else:
        sources[index] = value
    return SourceDraft(settings.revision, tuple(sources))


def table_document(
    table: TableEdit, kind: SourceKind, section: str, old: dict[str, object]
) -> dict[str, object]:
    """조회한 표의 선택값을 검증하고 같은 표의 레거시 동작을 보존한다."""
    values = table.values
    validate_roles(table.roles, values)
    value: dict[str, object] = {
        "provider": "notion",
        "kind": kind,
        "dataSourceId": table.id,
        "properties": values,
        "columnRoles": roles_payload(table.roles),
        "columnLabels": table.labels,
    }
    if table.parent_title is not None:
        value["parentTitleProperty"] = table.parent_title
    # 새 표에는 이전 표의 완료·일정 분류나 레거시 필수 역할을 넘기지 않는다.
    stays_legacy = (
        old.get("kind") == kind
        and old.get("flexible") is not True
        and all(role.key in values for role in table.roles if role.required)
        and (table.parent_title is None or section == "qa")
    )
    if not stays_legacy:
        value["flexible"] = True
    if kind == "meeting":
        # 기존 표의 기본 유형을 보존하고 새 표는 명시적으로 연결한 값만 회의로 본다.
        value["defaultEventType"] = old.get(
            "defaultEventType", "meeting" if old.get("kind") == "meeting" else "other"
        )
    choices = table.choices
    if kind == "meeting" and "category" in values:
        value["eventTypes"] = {
            name: EVENT_CHOICES[index] for name, index in choices.items() if index
        }
    if kind == "leave":
        selected = [name for name, index in choices.items() if index == 1]
        if not selected:
            raise SourceSettingsError("승인된 휴가에 쓰는 상태를 하나 이상 선택하세요.")
        value["approvedValues"] = selected
    if kind == "task" and "status" in values:
        completed = [name for name, index in choices.items() if index]
        if completed or "completedStatuses" in old:
            value["completedStatuses"] = completed
    return value


def validate_roles(roles: tuple[ColumnRole, ...], values: dict[str, str]) -> None:
    """사용할 역할의 이름과 필수 원본 칼럼을 검사한다."""
    if any(not 0 < len(role.title) <= 80 for role in roles):
        raise SourceSettingsError("역할 이름은 1~80자로 입력하세요.")
    optional = {"assignees", "participants", "status"}
    for role in roles:
        if role.required and role.key not in optional and role.key not in values:
            raise SourceSettingsError(f"'{role.title}'에 사용할 원본 칼럼을 선택하세요.")
