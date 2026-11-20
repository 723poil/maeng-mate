"""실행본별 macOS 서비스와 개인 저장 공간의 이름을 결정한다."""

import plistlib
from dataclasses import dataclass
from functools import cache

from src.bootstrap.executable import bundle_contents, packaged


@dataclass(frozen=True)
class BuildProfile:
    """번들·로그인 서비스·Keychain이 공유하는 실행본 식별 정보다."""

    identifier: str
    support_folder: str
    port: int


RELEASE = BuildProfile("local.maeng-mate", "MaengMate", 43127)
DEVELOPMENT = BuildProfile("local.maeng-mate.dev", "MaengMateDev", 43128)


@cache
def current_profile() -> BuildProfile:
    """소스와 로컬 패키지는 개발용, 업데이트 공개키를 동봉한 앱은 배포용이다."""
    if not packaged():
        return DEVELOPMENT
    with (bundle_contents() / "Info.plist").open("rb") as stream:
        info = plistlib.load(stream)
    return RELEASE if info.get("SUPublicEDKey") else DEVELOPMENT
