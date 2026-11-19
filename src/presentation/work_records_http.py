"""관리자용 출퇴근 기록 조회·명시적 정정 경로다."""

import json
from datetime import date

from fastapi import APIRouter, HTTPException, Request

from src.application.day_ports import DayConflictError
from src.application.errors import SnapshotError
from src.application.work_records import WorkRecordsService
from src.presentation.request_body import read_limited_body


def create_work_records_router(service: WorkRecordsService | None) -> APIRouter:
    """인증된 앱 관리 요청만 받고 저장·조회 구현은 조립부에서 받는다."""
    router = APIRouter()

    @router.get("/v1/work-records")
    async def month(month: str | None = None) -> dict[str, object]:
        """선택 월의 실제 기록을 읽으며 예상 시각을 섞지 않는다."""
        if service is None:
            raise HTTPException(503, "기록 DB가 필요합니다.")
        try:
            return (await service.month(month)).payload()
        except SnapshotError:
            raise HTTPException(503, "출퇴근 기록을 읽지 못했습니다.") from None
        except ValueError:
            raise HTTPException(422, "조회 월을 확인하세요.") from None

    @router.get("/v1/work-records/{day}")
    async def detail(day: date) -> dict[str, object]:
        """현재 버전과 수정 이력만 반환하고 메모·규칙은 제외한다."""
        if service is None:
            raise HTTPException(503, "기록 DB가 필요합니다.")
        try:
            return (await service.detail(day)).payload()
        except SnapshotError:
            raise HTTPException(503, "수정 이력을 읽지 못했습니다.") from None
        except ValueError:
            raise HTTPException(422, "기록 날짜를 확인하세요.") from None

    @router.put("/v1/work-records/{day}")
    async def correct(day: date, request: Request) -> dict[str, object]:
        """날짜·버전을 고정하고 작은 정정 본문만 원자 저장한다."""
        if service is None:
            raise HTTPException(503, "기록 DB가 필요합니다.")
        body = await read_limited_body(request, max_bytes=4096, too_large="입력이 너무 큽니다.")
        try:
            raw: object = json.loads(body)
            if not isinstance(raw, dict) or raw.get("date") != day.isoformat():
                raise ValueError
            return (await service.correct(raw)).payload()
        except DayConflictError:
            raise HTTPException(409, "기록이 변경됐습니다. 다시 불러와 주세요.") from None
        except SnapshotError:
            raise HTTPException(503, "저장 결과를 확인하지 못했습니다.") from None
        except (ValueError, UnicodeError):
            raise HTTPException(
                422,
                "날짜·출근·퇴근·휴가를 확인하세요. 퇴근은 출근 이후이며 현재 시각 이전이어야 합니다.",
            ) from None

    @router.post("/v1/work-records/{day}/confirm")
    async def confirm(day: date, request: Request) -> dict[str, object]:
        """별도 확정 동작은 버전만 받아 서버 계산으로 퇴근을 기록한다."""
        if service is None:
            raise HTTPException(503, "기록 DB가 필요합니다.")
        body = await read_limited_body(request, max_bytes=256, too_large="입력이 너무 큽니다.")
        try:
            raw: object = json.loads(body)
            if (
                not isinstance(raw, dict)
                or set(raw) != {"revision"}
                or type(raw["revision"]) is not int
            ):
                raise ValueError
            return (await service.confirm(day, raw["revision"])).payload()
        except DayConflictError:
            raise HTTPException(
                409, "이미 확정됐거나 기록이 변경됐습니다. 다시 불러와 주세요."
            ) from None
        except SnapshotError:
            raise HTTPException(503, "확정 결과를 확인하지 못했습니다.") from None
        except (ValueError, UnicodeError):
            raise HTTPException(422, "퇴근 예정시간이 지난 기록만 확정할 수 있습니다.") from None

    return router
