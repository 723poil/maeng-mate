"""출퇴근 기록 창의 관리자용 루프백 HTTP 클라이언트다."""

from datetime import date

import httpx

from src.application.date_inputs import parse_month
from src.application.day_ports import DayConflictError, DayInputError
from src.application.errors import StatusReadError
from src.application.work_records import (
    RecordRevision,
    WorkRecordCorrection,
    WorkRecordDetail,
    WorkRecordItem,
    WorkRecordsMonth,
)
from src.domain.daily_work import parse_clock, parse_leave
from src.infrastructure.api_decoding import boolean, instant, integer, json_object, strings, text
from src.infrastructure.local_http import local_client, response_json


def _item(value: object) -> WorkRecordItem:
    """시간대 없는 실제 시·분과 날짜·버전의 타입을 확인한다."""
    raw = json_object(value)
    day = date.fromisoformat(text(raw["date"]))
    arrival, departure = raw["attendanceTime"], raw["departureTime"]
    for clock in (arrival, departure):
        if clock is not None:
            parse_clock(clock)
    return WorkRecordItem(
        day,
        integer(raw["revision"]),
        text(arrival) if arrival is not None else None,
        text(departure) if departure is not None else None,
        parse_leave(raw["leaveType"]),
        instant(raw["updatedAt"]),
        boolean(raw.get("overtime", False)),
    )


def _detail(value: object) -> WorkRecordDetail:
    """최대50회 이력만 받아 잘못된 성공 응답을 표시하지 않는다."""
    raw = json_object(value)
    history = raw["history"]
    if not isinstance(history, list) or len(history) > 50:
        raise ValueError
    revisions: list[RecordRevision] = []
    for entry in history:
        item = json_object(entry)
        revisions.append(
            RecordRevision(
                integer(item["revision"]), instant(item["updatedAt"]), strings(item["changes"])
            )
        )
    return WorkRecordDetail(
        _item(raw["record"]),
        tuple(revisions),
        boolean(raw["truncated"]),
        instant(raw.get("expectedDepartureAt")),
        boolean(raw.get("canConfirm", False)),
        boolean(raw.get("canEdit", True)),
    )


class WorkRecordsApi:
    """DB·비밀 파일 없이 앱 내부 관리 API만 사용한다."""

    def __init__(self, address: str, token: str) -> None:
        """조립부가 검증한 루프백 주소와 관리 키만 받는다."""
        self._address, self._token = address, token

    def _request(
        self,
        path: str,
        *,
        month: str | None = None,
        correction: WorkRecordCorrection | None = None,
        confirm_revision: int | None = None,
    ) -> dict[str, object]:
        """조회·저장을 자동 재전송하지 않고 안전한 오류로 구분한다."""
        try:
            with local_client(self._address, self._token, timeout=4) as client:
                if confirm_revision is not None:
                    response = client.post(path, json={"revision": confirm_revision})
                elif correction is not None:
                    response = client.put(path, json=correction.payload())
                else:
                    response = client.get(path, params={"month": month} if month else None)
                if response.status_code == 409:
                    raise DayConflictError()
                if response.status_code in {413, 422}:
                    raise DayInputError()
                response.raise_for_status()
                raw: object = response_json(response, max_bytes=65536)
                return json_object(raw)
        except (DayConflictError, DayInputError):
            raise
        except (httpx.HTTPError, ValueError):
            raise StatusReadError("기록을 확인하지 못했습니다. 다시 불러와 주세요.") from None

    def month(self, month: str | None = None) -> WorkRecordsMonth:
        """월별 목록의 날짜·중복·순서를 확인한다."""
        try:
            raw = self._request("/v1/work-records", month=month)
            key, today = text(raw["month"]), date.fromisoformat(text(raw["today"]))
            first, last = parse_month(key)
            records = raw["records"]
            if (
                not isinstance(records, list)
                or len(records) > 31
                or (month is not None and key != month)
            ):
                raise ValueError
            items = tuple(_item(item) for item in records)
            days = tuple(item.day for item in items)
            if (
                len(set(days)) != len(days)
                or days != tuple(sorted(days, reverse=True))
                or any(not first <= day <= last or day > today for day in days)
            ):
                raise ValueError
            return WorkRecordsMonth(key, today, items)
        except (KeyError, ValueError):
            raise StatusReadError("월별 기록 응답을 확인하지 못했습니다.") from None

    def detail(self, day: date) -> WorkRecordDetail:
        """선택 날짜와 다른 상세 응답은 거부한다."""
        try:
            result = _detail(self._request("/v1/work-records/" + day.isoformat()))
            if result.item.day != day:
                raise ValueError
            return result
        except (KeyError, ValueError):
            raise StatusReadError("수정 이력을 확인하지 못했습니다.") from None

    def save(self, value: WorkRecordCorrection) -> WorkRecordDetail:
        """성공 버전을 확인하고 실패한 쓰기를 다시 시도하지 않는다."""
        try:
            result = _detail(
                self._request("/v1/work-records/" + value.day.isoformat(), correction=value)
            )
            if result.item.day != value.day or result.item.revision <= value.revision:
                raise ValueError
            return result
        except (DayConflictError, DayInputError):
            raise
        except (KeyError, ValueError):
            raise StatusReadError("저장 여부를 확인하지 못했습니다. 다시 불러와 주세요.") from None

    def confirm(self, day: date, revision: int) -> WorkRecordDetail:
        """서버가 계산한 예정시간으로 한 번만 확정하고 성공 버전을 확인한다."""
        try:
            result = _detail(
                self._request(
                    "/v1/work-records/" + day.isoformat() + "/confirm", confirm_revision=revision
                )
            )
            if (
                result.item.day != day
                or result.item.revision <= revision
                or result.item.departure is None
            ):
                raise ValueError
            return result
        except (DayConflictError, DayInputError):
            raise
        except (KeyError, ValueError):
            raise StatusReadError("확정 여부를 확인하지 못했습니다. 다시 불러와 주세요.") from None
