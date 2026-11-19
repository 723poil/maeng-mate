"""메뉴바를 위한 인증된 루프백 API 어댑터다."""

import httpx

from src.application.date_inputs import parse_day
from src.application.day_ports import DayConflictError, DayInputError
from src.application.desktop import (
    CollectionView,
    DayUpdate,
    DayView,
    DesktopArea,
    DesktopSnapshot,
    MenuEntry,
)
from src.application.errors import StatusReadError
from src.application.notion_links import notion_link
from src.domain.work_dates import SEOUL
from src.infrastructure.api_decoding import (
    boolean,
    instant,
    integer,
    json_object,
    parse_display_fields,
    strings,
    text,
)
from src.infrastructure.day_response import day_view
from src.infrastructure.local_http import local_client, response_json


def deadline_label(value: object) -> str | None:
    """날짜는 그대로 보존하고 시점 기한만 서울 시각으로 변환한다."""
    if value is None:
        return None
    raw = json_object(value)
    start, end = raw.get("start"), raw.get("end")
    if raw.get("kind") == "date":
        first = parse_day(text(start))
        last = parse_day(text(end)) if end is not None else first
        if last < first:
            raise ValueError
        return last.strftime("%Y.%m.%d")
    if raw.get("kind") == "instant":
        first_instant = instant(start)
        last_instant = instant(end) if end is not None else first_instant
        if first_instant is None or last_instant is None or last_instant < first_instant:
            raise ValueError
        return last_instant.astimezone(SEOUL).strftime("%Y.%m.%d %H:%M")
    raise ValueError


def entries(
    value: object, *, calendar: bool | None, reference_day: str | None = None
) -> tuple[MenuEntry, ...]:
    """일정은 서울 시각으로 표시하며 날짜 전용 구간은 종일로 구별한다."""
    if not isinstance(value, list):
        raise ValueError
    result: list[MenuEntry] = []
    for item in value:
        raw = json_object(item)
        display_fields = parse_display_fields(raw.get("displayFields"))
        title = text(raw["title"])
        if "cancelled" in raw and boolean(raw["cancelled"]):
            title = f"[취소] {title}"
        detail = ""
        task_status: str | None = None
        deadline: str | None = None
        if calendar or (calendar is None and "period" in raw):
            period = raw.get("period")
            if period is None:
                detail = "시간 미정"
            else:
                period_raw = json_object(period)
                if period_raw.get("kind") == "date":
                    start_day = parse_day(text(period_raw["start"])).isoformat()
                    detail = "종일"
                else:
                    start = instant(period_raw.get("start"))
                    end = instant(period_raw.get("end"))
                    start_day = start.astimezone(SEOUL).date().isoformat() if start else ""
                    detail = start.astimezone(SEOUL).strftime("%H:%M") if start else "시간 미정"
                    if end:
                        detail += "–" + end.astimezone(SEOUL).strftime("%H:%M")
                # 조회일 밖의 일정도 포함하는 카드에서는 어느 날의 일정인지 보여 준다.
                if reference_day and start_day and start_day != reference_day:
                    detail = f"{start_day} · {detail}"
        else:
            status = raw.get("status")
            detail = (
                text(status)
                if status is not None
                else "상태 미정"
                if raw.get("tracksStatus") is not False
                else ""
            )
            task_status = detail or None
            deadline = deadline_label(raw.get("due"))
        result.append(
            MenuEntry(
                title,
                detail,
                notion_link(raw.get("url")),
                task_status,
                deadline,
                text(raw["qaRound"]) if raw.get("qaRound") is not None else None,
                text(raw["sourceId"]) if raw.get("sourceId") is not None else None,
                display_fields,
            )
        )
    return tuple(result)


def primary_board(value: object) -> str | None:
    """설정 순서의 첫 보드를 사용하되 전달된 모든 URL의 범위를 검사한다."""
    if not isinstance(value, list):
        raise ValueError
    links = [notion_link(item) for item in value]
    if any(link is None for link in links):
        raise ValueError
    return links[0] if links else None


def freshness_reasons(value: object) -> tuple[str, ...]:
    """소스 식별자는 UI로 넘기지 않고 갱신 지연·실패 이유만 모은다."""
    if not isinstance(value, list):
        raise ValueError
    reasons: list[str] = []
    for source in value:
        reasons.extend(strings(json_object(source)["reasons"]))
    return tuple(dict.fromkeys(reasons))


def desktop_areas(
    value: object, *, reference_day: str, mixed: bool = False
) -> dict[str, DesktopArea]:
    """메인 독립 영역에 필요한 미리보기 항목만 읽는다."""
    result = {}
    for key, raw in json_object(value).items():
        area = json_object(raw)
        result[key] = DesktopArea(
            entries(
                area["items"],
                calendar=None if mixed else boolean(area["calendar"]),
                reference_day=reference_day,
            ),
            integer(area["count"]),
        )
    return result


class DesktopApi:
    """DB·Notion 자격정보 없이 공통 로컬 포트·토큰만 사용한다."""

    def __init__(
        self, address: str, token: str, *, transport: httpx.BaseTransport | None = None
    ) -> None:
        """bootstrap에서 검증한 루프백 주소와 테스트 전송을 받는다."""
        self._address = address
        self._token = token
        self._transport = transport

    def _client(self) -> httpx.Client:
        """프록시·리다이렉트를 끄고 GUI 종료를 위해 요청 대기를 제한한다."""
        return local_client(self._address, self._token, timeout=3, transport=self._transport)

    def _payload(self, response: httpx.Response) -> dict[str, object]:
        """응답 크기와 JSON 객체 여부를 확인한다."""
        response.raise_for_status()
        value: object = response_json(response, max_bytes=1024 * 1024)
        return json_object(value)

    def fetch(self) -> DesktopSnapshot:
        """같은 날짜의 개인 기록·Notion 자료와 안전한 수집 집계를 읽는다."""
        try:
            with self._client() as client:
                day = day_view(self._payload(client.get("/v1/day")))
                status = self._payload(client.get("/v1/status", params={"desktop": True}))
                response = client.get("/v1/today", params={"date": day.update.day, "desktop": True})
                work_error = response.status_code == 503
                today = self._payload(response) if not work_error else None
            periodic = status.get("periodicSync")
            if periodic is None:
                collection = CollectionView(False, False, 0, 0, None, None)
            else:
                raw = json_object(periodic)
                collection = CollectionView(
                    boolean(raw["enabled"]),
                    boolean(raw["running"]),
                    integer(raw["succeededSources"]),
                    integer(raw["failedSources"]),
                    instant(raw["nextRunAt"]),
                    instant(today["lastSuccessfulSyncAt"]) if today else None,
                    integer(raw["intervalSeconds"])
                    if raw.get("intervalSeconds") is not None
                    else None,
                    integer(raw.get("retryingSources", 0)),
                    integer(raw.get("maxRetryAttempt", 0)),
                    active_source_id=text(raw["activeSourceId"])
                    if raw.get("activeSourceId") is not None
                    else None,
                )
            if today is None:
                return DesktopSnapshot(
                    day, collection, (), (), (), (0, 0, 0), True, ("today_unavailable",), True
                )
            if today["date"] != day.update.day:
                raise ValueError
            counts = json_object(today["totalCounts"])
            pages = json_object(today.get("sourcePages", {}))
            return DesktopSnapshot(
                day,
                collection,
                entries(today["tasks"], calendar=False),
                entries(today["meetings"], calendar=True),
                entries(today["leaves"], calendar=True),
                (integer(counts["tasks"]), integer(counts["meetings"]), integer(counts["leaves"])),
                boolean(today["stale"]),
                strings(today["warnings"]),
                task_page=primary_board(pages.get("tasks", [])),
                meeting_page=primary_board(pages.get("meetings", [])),
                freshness_reasons=freshness_reasons(today.get("sources", [])),
                qa=entries(today.get("qa", []), calendar=False),
                qa_count=integer(counts.get("qa", 0)),
                qa_page=primary_board(pages.get("qa", [])),
                page_links_revision=integer(today.get("sourcePagesRevision", 0)),
                other_events=entries(today.get("otherEvents", []), calendar=True),
                source_areas=desktop_areas(
                    today.get("desktopAreas", {}), reference_day=day.update.day
                ),
                category_areas=desktop_areas(
                    today.get("categoryAreas", {}), reference_day=day.update.day, mixed=True
                ),
            )
        except (httpx.HTTPError, ValueError, KeyError, TypeError):
            raise StatusReadError(
                "로컬 앱 상태를 읽지 못했습니다. 연결과 DB 준비 상태를 확인하세요."
            ) from None

    def set_interval(self, seconds: int) -> DesktopSnapshot:
        """주기 쓰기는 한 번만 요청하고 응답 불명확 시 재시도를 강제하지 않는다."""
        try:
            with self._client() as client:
                self._payload(
                    client.put("/v1/collection/interval", json={"intervalSeconds": seconds})
                )
        except (httpx.HTTPError, ValueError, KeyError, TypeError):
            raise StatusReadError(
                "주기 저장을 확인하지 못했습니다. 새로고침해 확인하세요."
            ) from None
        return self.fetch()

    def save(self, update: DayUpdate) -> DayView:
        """쓰기 성공 응답으로만 갱신하고 충돌이나 오류는 재전송하지 않는다."""
        try:
            with self._client() as client:
                response = client.put("/v1/day", json=update.payload())
                if response.status_code == 409:
                    raise DayConflictError()
                if response.status_code in {413, 422}:
                    raise DayInputError()
                saved = day_view(self._payload(response))
                if saved.update.day != update.day or saved.update.revision != update.revision + 1:
                    raise ValueError
                return saved
        except (DayConflictError, DayInputError):
            raise
        except (httpx.HTTPError, ValueError, KeyError, TypeError):
            raise StatusReadError(
                "저장 여부를 확인하지 못했습니다. 새로고침 후 기록을 확인하세요."
            ) from None
