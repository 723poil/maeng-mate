"""기록 창의 월별 조회·선택·명시적 저장 상태를 관리한다."""

from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import replace
from datetime import date, datetime

from src.application.day_ports import DayInputError
from src.application.work_records import (
    WorkRecordCorrection,
    WorkRecordDetail,
    WorkRecordsClient,
    WorkRecordsMonth,
)
from src.domain.daily_work import departure_due
from src.domain.work_dates import SEOUL


class WorkRecordsController:
    """조회 중 기존 목록을 보존하고 저장 실패·충돌 시 초안을 유지한다."""

    def __init__(self, client: WorkRecordsClient, on_saved: Callable[[date], None]) -> None:
        """별도 네트워크 작업자와 오늘 기록 갱신 콜백을 주입한다."""
        self._client, self._on_saved = client, on_saved
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="work-records")
        self._month_pending: Future[WorkRecordsMonth] | None = None
        self._detail_pending: Future[WorkRecordDetail] | None = None
        self._save_pending: Future[WorkRecordDetail] | None = None
        self.month: WorkRecordsMonth | None = None
        self.detail: WorkRecordDetail | None = None
        self.selected: date | None = None
        self.dirty = False
        self.blocked = False
        self.error: str | None = None
        self.generation = 0
        self.month_generation = 0
        self._closed = False
        self.confirming = False

    @property
    def busy(self) -> bool:
        """조회·저장 중에는 다른 날짜의 입력이 섞이지 않게 한다."""
        return (
            self._month_pending is not None
            or self._detail_pending is not None
            or self._save_pending is not None
        )

    @property
    def saving(self) -> bool:
        """쓰기 진행과 단순 읽기를 별도 문구로 구분한다."""
        return self._save_pending is not None

    @property
    def editable(self) -> bool:
        """현재 선택 날짜의 버전을 읽은 뒤에만 수정한다."""
        return (
            self.detail is not None
            and self.detail.can_edit
            and self.detail.item.day == self.selected
            and not self.busy
            and not self.blocked
        )

    def load(self, month: str | None = None) -> None:
        """명시적 재조회에서만 실패한 초안을 확정값으로 되돌린다."""
        if self._closed or self.busy or self.dirty:
            return
        self.error = None
        self.blocked = False
        self._month_pending = self._executor.submit(self._client.month, month)

    def reload(self) -> None:
        """모호한 저장 결과를 명시적으로 다시 읽을 때만 초안을 초기화한다."""
        if self.busy or (self.dirty and not self.blocked):
            return
        self.dirty = False
        self.load(self.month.month if self.month else None)

    def select(self, day: date) -> None:
        """미저장 수정이 있을 때는 선택을 바꾸지 않는다."""
        if (
            self._closed
            or self.busy
            or self.dirty
            or self.month is None
            or day not in {item.day for item in self.month.items}
        ):
            return
        self.selected = day
        self.detail = None
        self.generation += 1
        self.error = None
        self.blocked = False
        self._detail_pending = self._executor.submit(self._client.detail, day)

    def edited(self) -> None:
        """자동 저장하지 않고 저장·취소 선택을 기다린다."""
        if self.editable:
            self.dirty = True
            self.error = None

    def cancel(self) -> None:
        """전송하지 않은 수정만 되돌리고 쓰기 결과 확인이 필요하면 재조회한다."""
        if self.busy:
            return
        self.dirty = False
        self.generation += 1
        if self.blocked and self.selected is not None:
            self.select(self.selected)
        else:
            self.error = None

    def save(self, value: WorkRecordCorrection) -> None:
        """선택 날짜·버전을 고정해 한 번만 저장한다."""
        if (
            not self.editable
            or self.detail is None
            or value.day != self.selected
            or value.revision != self.detail.item.revision
        ):
            return
        self.error = None
        self._save_pending = self._executor.submit(self._client.save, value)

    @property
    def can_confirm(self) -> bool:
        """미수정·미확정 기록만 허용하며 열린 창에서도 예정 시각 경과를 반영한다."""
        detail = self.detail
        if not self.editable or self.dirty or detail is None or detail.item.departure is not None:
            return False
        return detail.can_confirm or departure_due(
            detail.expected_departure, detail.item.day, datetime.now(SEOUL)
        )

    def confirm(self) -> None:
        """수정 저장과 구분된 버튼에서 선택 버전의 퇴근 예정시간을 확정한다."""
        if not self.can_confirm or self.detail is None:
            return
        self.error = None
        self.confirming = True
        item = self.detail.item
        self._save_pending = self._executor.submit(self._client.confirm, item.day, item.revision)

    def tick(self) -> None:
        """메인 스레드에서만 응답과 선택을 갱신한다."""
        if self._closed:
            return
        if self._month_pending is not None and self._month_pending.done():
            future, self._month_pending = self._month_pending, None
            try:
                self.month = future.result()
                self.month_generation += 1
                self.detail = None
                selected = (
                    self.selected
                    if self.selected in {item.day for item in self.month.items}
                    else self.month.items[0].day
                    if self.month.items
                    else None
                )
                self.selected = selected
                self.generation += 1
                if selected is not None:
                    self.select(selected)
            except Exception:
                self.error = "월별 기록을 불러오지 못했습니다. 다시 불러와 주세요."
        if self._detail_pending is not None and self._detail_pending.done():
            pending, self._detail_pending = self._detail_pending, None
            try:
                self.detail = pending.result()
                self.generation += 1
            except Exception:
                self.blocked = True
                self.error = "선택한 기록을 불러오지 못했습니다. 다시 불러와 주세요."
        if self._save_pending is not None and self._save_pending.done():
            saving, self._save_pending = self._save_pending, None
            confirming, self.confirming = self.confirming, False
            try:
                self.detail = saving.result()
                self.generation += 1
                self.dirty = False
                if self.month is not None:
                    self.month = replace(
                        self.month,
                        items=tuple(
                            self.detail.item if item.day == self.detail.item.day else item
                            for item in self.month.items
                        ),
                    )
                    self.month_generation += 1
                self._on_saved(self.detail.item.day)
            except DayInputError:
                self.error = (
                    "퇴근 예정시간이 지난 기록만 확정할 수 있습니다."
                    if confirming
                    else (
                        "출근·퇴근·휴가를 확인하세요. 퇴근은 출근 이후·현재 시각 이전으로 입력하세요."
                    )
                )
            except Exception:
                self.blocked = True
                self.error = (
                    "다시 불러와 퇴근이 확정됐는지 확인하세요."
                    if confirming
                    else "다시 불러와 기록이 저장됐는지 확인하세요."
                )

    @property
    def message(self) -> str:
        """진행·실패·미저장 상태를 짧게 표시한다."""
        return self.error or (
            "확정 중…"
            if self.confirming
            else "저장 중…"
            if self.saving
            else "불러오는 중…"
            if self.busy
            else "수정 중 · 저장 또는 취소"
            if self.dirty
            else "확정됨"
            if self.detail and self.detail.item.departure is not None
            else "미확정"
            if self.detail and self.detail.expected_departure is not None
            else "저장됨"
            if self.detail and self.detail.item.revision
            else "미기록"
            if self.detail
            else "날짜를 선택하세요."
        )

    def close(self) -> None:
        """앱 종료 시 추가 조회·저장을 중단한다."""
        self._closed = True
        self._executor.shutdown(wait=False, cancel_futures=True)
