"""월별 달력의 관리자용 읽기 API다."""

import json

from fastapi import APIRouter, HTTPException

from src.application.calendar import CalendarService
from src.application.errors import QueryReadError, SnapshotError
from src.domain.calendar import CalendarItem, CalendarMonth


def calendar_payload(value: CalendarMonth) -> dict[str, object]:
    """원문 근무 규칙·사용자 ID·소스 매핑 없이 필요한 달력 값만 전달한다."""
    items = []
    detail_budget = 900_000
    truncated = value.truncated
    for item in value.items:
        row = calendar_item_payload(item)
        if item.display_fields is not None:
            fields = [{"label": name, "value": text} for name, text in item.display_fields]
            size = len(json.dumps(fields, ensure_ascii=True))
            if size <= detail_budget:
                detail_budget -= size
                row["displayFields"] = fields
            else:
                row["displayFields"] = []
                truncated = True  # 월 격자와 제목은 보존하고 긴 상세만 한도를 둔다.
        items.append(row)
    return {
        "month": value.month,
        "today": value.today.isoformat(),
        "first": value.first.isoformat(),
        "last": value.last.isoformat(),
        "asOf": value.observed_at.isoformat(),
        "stale": value.stale,
        "truncated": truncated,
        "sourceConfigured": value.source_configured,
        "items": items,
        "attendance": [
            {
                "date": item.day.isoformat(),
                "arrival": item.arrival,
                "departure": item.departure,
                "leave": item.leave.value,
                "overtime": item.overtime,
            }
            for item in value.attendance
        ],
    }


def calendar_item_payload(item: CalendarItem) -> dict[str, object]:
    """날짜 격자와 원본을 여는 데 필요한 고정 정보를 구성한다."""
    return {
        "key": item.key,
        "kind": item.kind,
        "color": item.color,
        "categoryKey": item.category_key,
        "title": item.title,
        "first": item.first.isoformat(),
        "last": item.last.isoformat(),
        "startAt": item.start_at.isoformat() if item.start_at else None,
        "endAt": item.end_at.isoformat() if item.end_at else None,
        "allDay": item.all_day,
        "url": item.url,
        "status": item.status,
        "startDate": item.start_date.isoformat() if item.start_date else None,
        "dueDate": item.due_date.isoformat() if item.due_date else None,
    }


def create_calendar_router(service: CalendarService | None) -> APIRouter:
    """조회에 설정 파일·SDK 조립·수집 시작 권한을 주지 않는다."""
    router = APIRouter()

    @router.get("/v1/calendar")
    async def month(month: str | None = None) -> dict[str, object]:
        """현재 스냅샷과 실제 출퇴근의 월별 표시 자료만 읽는다."""
        if service is None:
            raise HTTPException(503, "캘린더 DB가 필요합니다.")
        try:
            result = calendar_payload(await service.month(month))
            if len(json.dumps(result, ensure_ascii=True).encode()) > 2 * 1024 * 1024:
                raise QueryReadError()
            return result
        except (QueryReadError, SnapshotError):
            raise HTTPException(503, "저장된 캘린더 자료를 읽지 못했습니다.") from None
        except (ValueError, OverflowError):
            raise HTTPException(422, "조회 월을 확인하세요.") from None

    return router
