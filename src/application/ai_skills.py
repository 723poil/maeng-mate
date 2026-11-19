"""앱이 제공하는 조회 지침의 설치 대상과 결과 계약이다."""

from dataclasses import dataclass
from enum import Enum
from typing import Literal, Protocol

SkillTarget = Literal["codex", "claude", "both"]


class UnversionedSkill(Enum):
    """설치 버전 번호를 표시할 수 없는 이유를 구분한다."""

    MISSING = "missing"
    LEGACY = "legacy"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class SkillVersions:
    """앱에 동봉한 버전과 각 AI 설치 기록의 버전을 전달한다."""

    bundled: str
    codex: str | UnversionedSkill
    claude: str | UnversionedSkill

    def update_available(self, installed: str) -> bool:
        """숫자 세 부분을 비교해 1.10.0처럼 자릿수가 늘어난 버전도 구분한다."""
        return version_numbers(installed) < version_numbers(self.bundled)


def version_numbers(version: str) -> tuple[int, ...]:
    """검증된 배포 번호를 숫자 순서로 비교할 수 있게 변환한다."""
    return tuple(map(int, version.split(".")))


class SkillInstallError(Exception):
    """설치 경로·사용자 파일 보호 실패를 원문 없이 구분한다."""

    def __init__(self, code: str) -> None:
        """파일 내용이나 개인 경로가 메시지로 섞이지 않게 한다."""
        self.code = code if code in {"modified", "path", "newer"} else "storage"
        super().__init__(self.code)


@dataclass(frozen=True)
class SkillInstallResult:
    """비밀값이나 개인 경로 없는 설치 완료 정보다."""

    targets: tuple[str, ...]
    version: str


class SkillInstaller(Protocol):
    """프로바이더 연결 설정과 분리된 지침 설치 포트다."""

    def versions(self) -> SkillVersions:
        """파일을 변경하지 않고 대상별 설치 버전을 조회한다."""
        ...

    def install(self, target: SkillTarget) -> SkillInstallResult:
        """고정된 대상에만 스킬을 설치하고 사용자 변경은 보존한다."""
        ...
