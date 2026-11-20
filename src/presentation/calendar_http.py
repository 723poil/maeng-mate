"""월별 달력의 관리자용 읽기 API다."""

import json
from json.encoder import encode_basestring_ascii

from fastapi import APIRouter, HTTPException

from src.application.calendar import CalendarService
from src.application.errors import QueryReadError, SnapshotError
from src.domain.calendar import CalendarItem, CalendarMonth

# 문자열 자체(양쪽 따옴표 포함)를 제외한 필드 객체의 고정 JSON 길이다.
_FIELD_OVERHEAD = len(json.dumps({"label": "", "value": ""})) - 2 * len('""')


def detail_size_within_budget(
    fields: tuple[tuple[str, str], ...], budget: int, badge_count: int = 0
) -> int | None:
    """남은 한도를 넘으면 그 행의 나머지 칼럼은 변환하지 않는다."""
    # 배열·구분자·객체 키의 고정 비용은 문자열을 읽기 전에 알 수 있다.
    size = len("[]") + max(0, len(fields) - 1) * len(", ") + len(fields) * _FIELD_OVERHEAD
    size += badge_count * len(', "badge": true')
    if size > budget:
        return None
    for name, value in fields:
        # 표준 JSON 이스케이프를 적용해 한글·이모지·제어문자도 기존 예산과 똑같이 센다.
        size += len(encode_basestring_ascii(name)) + len(encode_basestring_ascii(value))
        if size > budget:
            return None
    return size


def calendar_payload(value: CalendarMonth) -> dict[str, object]:
    """원문 근무 규칙·사용자 ID·소스 매핑 없이 필요한 달력 값만 전달한다."""
    items = []
    detail_budget = 900_000
    truncated = value.truncated

    def serialize(item: CalendarItem) -> dict[str, object]:
        """같은 일정의 모든 표시 후보가 월 응답의 상세 크기 한도를 공유한다."""
        nonlocal detail_budget, truncated
        row = calendar_item_payload(item)
        if item.display_fields is not None:
            size = detail_size_within_budget(
                item.display_fields, detail_budget, len(item.badge_field_indices)
            )
            if size is not None:
                detail_budget -= size
                row["displayFields"] = [
                    {
                        "label": name,
                        "value": text,
                        **({"badge": True} if index in item.badge_field_indices else {}),
                    }
                    for index, (name, text) in enumerate(item.display_fields)
                ]
            else:
                row["displayFields"] = []
                truncated = True  # 월 격자와 제목은 보존하고 긴 상세만 한도를 둔다.
        return row

    for item in value.items:
        row = serialize(item)
        if item.alternatives:
            row["alternatives"] = [serialize(variant) for variant in item.alternatives]
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
        **({"category": item.category} if item.category is not None else {}),
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
            if len(json.dumps(result, ensure_ascii=True)) > 2 * 1024 * 1024:
                raise QueryReadError()
            return result
        except (QueryReadError, SnapshotError):
            raise HTTPException(503, "저장된 캘린더 자료를 읽지 못했습니다.") from None
        except (ValueError, OverflowError):
            raise HTTPException(422, "조회 월을 확인하세요.") from None

    return router
