"""카테고리에서 선택해 등록할 수 있는 앱 이벤트 조건이다."""

from dataclasses import dataclass


@dataclass(frozen=True)
class EventAlertOption:
    """사용자가 조건으로 고를 수 있는 이벤트와 라우팅 키다."""

    trigger: str
    condition: str
    kinds: tuple[str, ...]


EVENT_ALERT_OPTIONS = (
    EventAlertOption("calendar", "내가 일정 참여자로 추가될 때", ("calendar",)),
    EventAlertOption("meeting", "내가 참석할 회의 시작 10분 전", ("meeting",)),
    EventAlertOption("assigned", "내가 담당자로 추가될 때", ("task_assigned", "qa_assigned")),
    EventAlertOption("status", "내 담당 항목의 상태가 바뀔 때", ("task_status", "qa_status")),
    EventAlertOption("due", "내 담당 항목의 기한이 바뀔 때", ("task_due", "qa_due")),
    EventAlertOption("sync_error", "수집에 실패했을 때", ("sync_error",)),
)
EVENT_ALERT_TRIGGERS = {item.trigger: item for item in EVENT_ALERT_OPTIONS}
EVENT_ALERT_KINDS = {kind: item.trigger for item in EVENT_ALERT_OPTIONS for kind in item.kinds}


@dataclass(frozen=True)
class CategoryEventAlert:
    """등록된 카테고리에 사용자가 붙인 이름과 발송 조건이다."""

    trigger: str
    name: str


@dataclass(frozen=True)
class EventAlertSelection:
    """카테고리 규칙과 같은 저장에서 반영할 스위치 변경이다."""

    category_id: str
    trigger: str
    enabled: bool
