"""macOS 사용자 LaunchAgent의 생성과 등록을 담당한다."""

import plistlib
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from src.infrastructure.private_files import write_private_file


class LaunchdError(RuntimeError):
    """명령 오류에 개인 경로나 환경을 노출하지 않는다."""


@dataclass(frozen=True)
class AgentDefinition:
    """토큰 없이 등록할 실행 명령과 로컬 경로다."""

    label: str
    arguments: tuple[str, ...]
    working_directory: Path
    log: Path
    process_type: Literal["Background", "Interactive"] = "Background"

    def payload(self) -> bytes:
        """정상 종료는 유지하고 실패한 작업만 다시 시작하도록 작성한다."""
        return plistlib.dumps(
            {
                "Label": self.label,
                "ProgramArguments": list(self.arguments),
                "WorkingDirectory": str(self.working_directory),
                "RunAtLoad": True,
                "KeepAlive": {"SuccessfulExit": False},
                "ThrottleInterval": 10,
                "ExitTimeOut": 15,
                "LimitLoadToSessionType": "Aqua",
                "ProcessType": self.process_type,
                "Umask": 0o077,
                "EnvironmentVariables": {"PYTHONUNBUFFERED": "1"},
                "StandardOutPath": str(self.log),
                "StandardErrorPath": str(self.log),
            },
            sort_keys=False,
        )


class MacLaunchd:
    """현재 로그인 사용자의 서비스만 관리한다."""

    def __init__(self, uid: int, agents_directory: Path) -> None:
        """도메인과 등록 디렉터리는 부트스트랩에서 결정한다."""
        self.domain = f"gui/{uid}"
        self.directory = agents_directory

    def path(self, label: str) -> Path:
        """서비스 이름이 파일 경로 밖으로 벗어나지 못하게 한다."""
        if not label or any(
            character not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789.-"
            for character in label
        ):
            raise LaunchdError("올바르지 않은 서비스 이름입니다.")
        return self.directory / (label + ".plist")

    def _command(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        """launchctl의 원문 출력은 반환만 하고 로그나 오류에 복사하지 않는다."""
        try:
            return subprocess.run(
                ["/bin/launchctl", *arguments],
                capture_output=True,
                text=True,
                timeout=25,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            raise LaunchdError("launchd 작업을 완료하지 못했습니다.") from None

    def _require(self, *arguments: str) -> str:
        """실패 원문 대신 작업 종류와 종료 코드만 공개한다."""
        result = self._command(*arguments)
        if result.returncode != 0:
            raise LaunchdError(f"launchd {arguments[0]} 실패 (코드 {result.returncode})")
        return result.stdout

    def registered(self, label: str) -> bool:
        """불안정한 print 텍스트를 파싱하지 않고 등록 여부만 확인한다."""
        return self._command("print", self.domain + "/" + label).returncode == 0

    def write(self, definition: AgentDefinition) -> None:
        """완성된 plist와 비공개 로그 파일을 준비한다."""
        self.path(definition.label)
        definition.log.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if not definition.log.exists():
            write_private_file(definition.log, b"")
        write_private_file(self.path(definition.label), definition.payload())

    def start(self, label: str) -> int:
        """중지된 자동 실행을 복원하고 이미 실행 중인 작업은 죽이지 않는다."""
        self._require("enable", self.domain + "/" + label)
        if not self.registered(label):
            self.load(label)
        # -p의 PID 출력은 문서화된 결과다. print의 내부 형식은 파싱하지 않는다.
        output = self._require("kickstart", "-p", self.domain + "/" + label).strip()
        if not output.isdecimal() or int(output) <= 0:
            raise LaunchdError("launchd 실행 PID를 확인하지 못했습니다.")
        return int(output)

    def load(self, label: str) -> None:
        """정의 파일을 현재 로그인 도메인에 등록한다."""
        self._require("bootstrap", self.domain, str(self.path(label)))

    def unload(self, label: str) -> None:
        """자동 실행 설정을 바꾸지 않고 현재 도메인에서만 제거한다."""
        if self.registered(label):
            self._require("bootout", self.domain + "/" + label)
            # 종료 중인 등록에 kickstart를 보내면 재실행 직후 함께 제거될 수 있다.
            deadline = time.monotonic() + 20
            while self.registered(label):
                if time.monotonic() >= deadline:
                    raise LaunchdError("이전 서비스 종료를 확인하지 못했습니다.")
                time.sleep(0.05)

    def stop(self, label: str) -> None:
        """명시적 중지는 다음 로그인에서도 꺼진 상태로 유지한다."""
        self._require("disable", self.domain + "/" + label)
        self.unload(label)

    def remove(self, label: str) -> None:
        """서비스 정의만 제거하고 앱 자료는 삭제하지 않는다."""
        self.stop(label)
        self.path(label).unlink(missing_ok=True)
