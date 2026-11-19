"""앱이 제공하는 조회 지침의 설치 대상과 결과 계약이다."""

from dataclasses import dataclass
from typing import Literal, Protocol

SkillTarget = Literal["codex", "claude", "both"]


class SkillInstallError(Exception):
    """설치 경로·사용자 파일 보호 실패를 원문 없이 구분한다."""

    def __init__(self, code: str) -> None:
        """파일 내용이나 개인 경로가 메시지로 섞이지 않게 한다."""
        self.code = code if code in {"modified", "path"} else "storage"
        super().__init__(self.code)


@dataclass(frozen=True)
class SkillInstallResult:
    """비밀값이나 개인 경로 없는 설치 완료 정보다."""

    targets: tuple[str, ...]


class SkillInstaller(Protocol):
    """프로바이더 연결 설정과 분리된 지침 설치 포트다."""

    def install(self, target: SkillTarget) -> SkillInstallResult:
        """고정된 대상에만 스킬을 설치하고 사용자 변경은 보존한다."""
        ...
