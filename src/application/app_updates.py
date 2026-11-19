"""설정에서 사용할 공개 버전 조회와 배포용 앱 설치 동작이다."""

from collections.abc import Callable
from dataclasses import dataclass


@dataclass(frozen=True)
class AppUpdates:
    """소스 실행과 배포 앱에 공통인 조회 및 가능한 설치 동작을 전달한다."""

    latest: Callable[[], str]
    open_release: Callable[[], None]
    install: Callable[[Callable[[], bool]], None] | None
