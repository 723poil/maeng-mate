"""개인 날짜별 기록의 저장·조회와 안전한 오류 계약이다."""

from datetime import date
from typing import Protocol

from src.domain.daily_work import DayRecord


class DayConflictError(ValueError):
    """다른 날짜 또는 다른 버전의 기록을 덮어쓰지 않게 한다."""

    def __init__(self) -> None:
        """사용자 입력 원문 없이 재확인을 요청한다."""
        super().__init__("날짜나 기록이 변경됐습니다. 새로고침 후 다시 확인하세요.")


class DayInputError(ValueError):
    """계획과 근무 시각 원문을 노출하지 않는 입력 오류다."""

    def __init__(self) -> None:
        """안전한 고정 안내만 반환한다."""
        super().__init__("날짜·시간·휴가 유형·계획 입력을 확인하세요.")


class NonWorkingDayError(DayInputError):
    """주말에는 개인 근무 입력을 저장하지 않는다."""

    def __init__(self) -> None:
        """고정된 주중 입력 안내만 반환한다."""
        ValueError.__init__(self, "근무 기록은 월요일부터 금요일에만 입력할 수 있습니다.")


class DayStore(Protocol):
    """날짜별 기록과 수정 이력을 원자적으로 보존한다."""

    async def read(self, day: date) -> DayRecord:
        """없는 기록은 현재 입력 규칙과 revision 0을 반환하며 파일을 만들지 않는다."""
        ...

    async def save(self, record: DayRecord, expected_revision: int) -> DayRecord:
        """날짜와 버전을 확인한 뒤 새 버전과 이력을 함께 저장한다."""
        ...


class DayUseCases(Protocol):
    """HTTP는 구체 저장소 대신 근무 조회·수정만 호출한다."""

    async def read(self) -> dict[str, object]:
        """오늘 기록과 계산 근거를 반환한다."""
        ...

    async def update(self, payload: object) -> dict[str, object]:
        """검증한 개인 입력만 수정한다."""
        ...
