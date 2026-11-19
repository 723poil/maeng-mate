"""메인과 별도 달력이 공유하는 월별 읽기·날짜 선택 상태다."""

import time
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import date

from src.application.calendar import CalendarClient
from src.domain.calendar import CalendarDay, CalendarMonth, CalendarPreferences


class CalendarController:
    """오래된 응답으로 다른 월을 덮지 않고 조회 실패 시 기존 자료를 유지한다."""

    def __init__(
        self, client: CalendarClient, *, notify_loaded: Callable[[], None] | None = None
    ) -> None:
        """읽기 클라이언트만 주입하고 보이지 않을 때는 조회하지 않는다."""
        self._client = client
        self._notify_loaded = notify_loaded
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="calendar-read")
        self._pending: Future[CalendarMonth] | None = None
        self._target_day: date | None = None
        self._closed = False
        self._last_read = 0.0
        self.data: CalendarMonth | None = None
        self._indexed_month: CalendarMonth | None = None
        self._indexed_preferences: CalendarPreferences | None = None
        self._days: dict[date, CalendarDay] = {}
        self.selected: date | None = None
        self.error: str | None = None
        self.generation = 0

    @property
    def busy(self) -> bool:
        """월 이동 중 두 화면의 날짜가 다른 자료와 섞이지 않게 한다."""
        return self._pending is not None

    def load(self, month: str | None = None, *, target_day: date | None = None) -> None:
        """현재 자료를 지우지 않고 지정 월을 한 번 읽는다."""
        if self.busy or self._closed:
            return
        self._target_day = target_day
        self.error = None
        self._pending = self._executor.submit(self._client.month, month)
        if self._notify_loaded is not None:
            self._pending.add_done_callback(self._notify_completion)

    def _notify_completion(self, future: Future[CalendarMonth]) -> None:
        """작업 스레드에서는 메인 루프에 완료 신호만 전달한다."""
        notify = self._notify_loaded
        if notify is not None and not future.cancelled():
            notify()

    def refresh(self) -> None:
        """선택 월을 다시 읽으며 수집이나 근무 저장을 시작하지 않는다."""
        self.load(self.data.month if self.data else None)

    def today(self) -> None:
        """서버의 오늘 기준 월로 돌아간다."""
        if self.busy:
            return
        self.selected = None
        self.load()

    def move(self, amount: int) -> None:
        """연도 경계를 포함해 이전·다음 월을 이동한다."""
        if self.data is None:
            return
        year, month = map(int, self.data.month.split("-"))
        index = year * 12 + month - 1 + amount
        if 1 <= index // 12 <= 9998:
            self.load(f"{index // 12:04d}-{index % 12 + 1:02d}")

    def select(self, day: date) -> None:
        """격자 밖 월 날짜는 그 월로 이동하고 같은 월은 즉시 선택한다."""
        if self.busy or self.data is None:
            return
        if day.strftime("%Y-%m") != self.data.month:
            self.load(day.strftime("%Y-%m"), target_day=day)
        elif self.selected != day:
            self.selected = day
            self.generation += 1

    def contents_on(self, day: date, preferences: CalendarPreferences) -> CalendarDay:
        """현재 월과 필터로 만든 날짜별 목록을 두 화면과 선택 날짜 상세가 공유한다."""
        data = self.data
        if data is None:
            return CalendarDay()
        if data is not self._indexed_month or preferences != self._indexed_preferences:
            self._days = data.visible_days(preferences)
            self._indexed_month = data
            self._indexed_preferences = preferences
        return self._days[day]

    def invalidate(self) -> None:
        """근무 정정 뒤 보이는 달력의 다음 읽기를 앞당긴다."""
        self._last_read = 0

    def apply_completed(self) -> bool:
        """완료 신호와 주기 타이머 중 먼저 도착한 쪽에서만 결과를 반영한다."""
        if self._closed or self._pending is None or not self._pending.done():
            return False
        pending, self._pending = self._pending, None
        self._last_read = time.monotonic()
        try:
            data = pending.result()
            selected = self._target_day or self.selected
            if selected is None or selected.strftime("%Y-%m") != data.month:
                selected = (
                    data.today
                    if data.today.strftime("%Y-%m") == data.month
                    else date.fromisoformat(data.month + "-01")
                )
            self.data, self.selected = data, selected
            self.error = None
            self.generation += 1
        except Exception:
            self.error = "캘린더를 읽지 못했습니다. 다시 불러와 주세요."
        return True

    def tick(self, *, visible: bool) -> None:
        """보이는 동안에만 자동 재조회하고 완료된 응답을 반영한다."""
        if self._closed:
            return
        self.apply_completed()
        if visible and not self.busy and time.monotonic() - self._last_read >= 60:
            self.refresh()

    @property
    def message(self) -> str:
        """자료 상태를 빈 일정과 구분한다."""
        if self.error:
            return self.error
        if self.busy:
            return "불러오는 중…"
        if self.data is None:
            return ""
        return " ".join(
            value
            for value in (
                "연결된 일정 소스가 없습니다."
                if not self.data.source_configured
                else "최신 일정을 확인하지 못했습니다."
                if self.data.stale
                else "",
                "일부 일정만 표시합니다." if self.data.truncated else "",
            )
            if value
        )

    def close(self) -> None:
        """앱 종료 시 추가 읽기를 중단한다."""
        self._closed = True
        self._notify_loaded = None
        self._executor.shutdown(wait=False, cancel_futures=True)
