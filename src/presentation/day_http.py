"""근무 기록의 HTTP 입출력과 안전한 오류 응답이다."""

import json

from fastapi import APIRouter, HTTPException, Request

from src.application.day_ports import (
    DayConflictError,
    DayInputError,
    DayUseCases,
    NonWorkingDayError,
)
from src.application.errors import SnapshotError
from src.presentation.request_body import read_limited_body


def create_day_router(service: DayUseCases | None) -> APIRouter:
    """구체 저장소를 만들지 않고 주입받은 기록 유스케이스만 호출한다."""
    router = APIRouter()

    @router.get("/v1/day")
    async def read_day(request: Request) -> dict[str, object]:
        """현재 서울 날짜의 개인 기록만 조회한다."""
        if service is None:
            raise HTTPException(503, detail="개인 기록 DB 설정이 필요합니다.")
        try:
            result = await service.read()
            if request.scope.get("maeng.mcp_read_only") is True:
                # 기존 stdio 응답 형태는 유지하되 메모·규칙 원문은 전달하지 않는다.
                public = {
                    key: result[key]
                    for key in (
                        "date",
                        "revision",
                        "attendanceTime",
                        "actualDepartureAt",
                        "overtime",
                        "leaveType",
                        "expectedDepartureAt",
                        "calculationState",
                        "requiredMinutes",
                        "lunchMinutes",
                        "workRulesConfigured",
                        "warnings",
                    )
                }
                public["plan"] = ""
                return public
            return result
        except SnapshotError:
            raise HTTPException(
                503, detail="개인 기록 DB를 준비하거나 연결을 확인하세요."
            ) from None

    @router.put("/v1/day")
    async def write_day(request: Request) -> dict[str, object]:
        """본문 크기를 제한하고 민감 입력을 오류 응답에 복사하지 않는다."""
        if service is None:
            raise HTTPException(503, detail="개인 기록 DB 설정이 필요합니다.")
        body = await read_limited_body(
            request, max_bytes=32768, too_large="입력 크기를 줄여 주세요."
        )
        try:
            payload: object = json.loads(body)
            return await service.update(payload)
        except NonWorkingDayError as error:
            raise HTTPException(422, detail=str(error)) from None
        except DayConflictError as error:
            raise HTTPException(409, detail=str(error)) from None
        except SnapshotError:
            raise HTTPException(503, detail="개인 기록을 저장하지 못했습니다.") from None
        except (ValueError, UnicodeError):
            raise HTTPException(422, detail=str(DayInputError())) from None

    return router
