"""실제 회사 연결 없이 설정 화면을 확인하기 위한 가상 업무 자료다."""

from dataclasses import replace
from datetime import datetime

from src.application.desktop import CollectionView, DayUpdate, DayView, DesktopSnapshot, MenuEntry
from src.domain.daily_work import LeaveType
from src.domain.preferences import Preferences
from src.domain.work_dates import SEOUL
from src.infrastructure.sqlite.connection import Database
from src.infrastructure.sqlite.preferences import SQLitePreferences


def enable_notifications(database: Database, *kinds: str) -> None:
    """임시 DB에 시나리오에 필요한 알림과 전달 채널만 명시한다."""
    SQLitePreferences(database).save(
        Preferences(notifications_enabled=True, channels=("macos",), notification_types=kinds)
    )


class PreferencesDesktopFixture:
    """출근 입력과 수집 주기 변경을 메모리에만 반영한다."""

    def __init__(self) -> None:
        """가짜 제목과 고정 순서를 가진 여러 영역을 준비한다."""
        day = datetime.now(SEOUL).date().isoformat()
        self.snapshot = DesktopSnapshot(
            DayView(
                DayUpdate(day, 0, None, LeaveType.NONE, ""), None, "unrecorded", None, 0, False, ()
            ),
            CollectionView(False, False, 0, 0, None, None),
            (
                MenuEntry("나 · 정산 검토", "진행 중", None, "진행 중", "2026.11.20"),
                MenuEntry("가 · 배포 확인", "대기", None, "대기", "2026.11.18"),
            ),
            (MenuEntry("가상 회의", "10:00", None),),
            (),
            (2, 1, 0),
            False,
            (),
            qa=tuple(
                MenuEntry(f"가상 QA {index:02d}", "대기", None, "대기", round_name="2차")
                for index in range(20)
            ),
            qa_count=20,
        )

    def fetch(self) -> DesktopSnapshot:
        """현재 가상 화면 자료를 반환한다."""
        return self.snapshot

    def set_interval(self, seconds: int) -> DesktopSnapshot:
        """설정 선택을 가상 수집 상태에만 반영한다."""
        self.snapshot = replace(
            self.snapshot, collection=replace(self.snapshot.collection, interval_seconds=seconds)
        )
        return self.snapshot

    def save(self, update: DayUpdate) -> DayView:
        """가상 근무 버전만 올리고 운영 기록은 읽지 않는다."""
        day = replace(self.snapshot.day, update=replace(update, revision=update.revision + 1))
        self.snapshot = replace(self.snapshot, day=day)
        return day
