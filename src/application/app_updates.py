"""설정에서 사용할 공개 버전 조회와 배포용 앱 설치 동작이다."""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol


class NativeUpdater(Protocol):
    """배포 앱의 자동 예약과 사용자 요청이 같은 설치 세션을 사용한다."""

    def start(self, pending: Callable[[], bool]) -> None:
        """설치 전 편집 상태를 확인할 수 있도록 자동 조회를 시작한다."""
        ...

    def check(self, pending: Callable[[], bool]) -> None:
        """사용자가 요청한 확인 창을 표시한다."""
        ...


@dataclass(frozen=True)
class AppUpdates:
    """소스 실행과 배포 앱에 공통인 조회 및 가능한 설치 동작을 전달한다."""

    latest: Callable[[], str]
    open_release: Callable[[], None]
    native: NativeUpdater | None
