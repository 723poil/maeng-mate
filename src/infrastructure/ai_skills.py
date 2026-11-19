"""번들 지침을 Codex·Claude Code 개인 스킬 폴더에 설치한다."""

import hashlib
import stat
from contextlib import ExitStack
from dataclasses import dataclass, field
from pathlib import Path

from src.application.ai_skills import SkillInstallError, SkillInstallResult, SkillTarget
from src.infrastructure.private_files import write_private_file
from src.infrastructure.skill_bundle import MARKER, bundled_files, install_marker, installed_hashes

_TARGETS = {"codex": ("Codex", ".agents"), "claude": ("Claude Code", ".claude")}


@dataclass(frozen=True)
class _InstallFile:
    """사전 검사한 원본과 이번에 설치할 내용을 한 파일 단위로 보관한다."""

    path: Path
    previous: bytes | None = field(repr=False)
    content: bytes = field(repr=False)

    def restore(self) -> None:
        """이번 작업이 쓴 내용만 되돌려 중간에 바뀐 사용자 파일을 보존한다."""
        if (
            self.path.is_symlink()
            or not self.path.is_file()
            or self.path.read_bytes() != self.content
        ):
            return
        if self.previous is None:
            self.path.unlink()
        else:
            write_private_file(self.path, self.previous)


def _remove_empty(directory: Path) -> None:
    """이번 설치가 만든 폴더도 다른 파일이 생겼으면 유지한다."""
    if not directory.is_symlink() and directory.is_dir() and not any(directory.iterdir()):
        directory.rmdir()


class UserSkillInstaller:
    """모든 대상의 사용자 변경을 확인한 뒤에만 파일 교체를 시작한다."""

    def __init__(self, home: Path) -> None:
        """설치 기준 홈을 시작 경계에서 주입한다."""
        self._home = home

    def _path(self, root: str) -> Path:
        """설치 대상의 상위 경로가 실제 디렉터리인지 확인한다."""
        path = self._home
        for part in (root, "skills", "maeng-mate"):
            path = path / part
            if path.is_symlink() or (path.exists() and not path.is_dir()):
                raise SkillInstallError("path")
        return path

    def _prepare(
        self, destination: Path, content: dict[str, bytes], manifest: bytes
    ) -> list[_InstallFile]:
        """기존 설치 해시와 실제 파일을 비교하고 대상별 교체 순서를 정한다."""
        marker = destination / MARKER
        if marker.is_symlink():
            raise SkillInstallError("path")
        previous_marker = marker.read_bytes() if marker.exists() else None
        expected = installed_hashes(previous_marker) if previous_marker is not None else {}
        if previous_marker is None and destination.exists() and any(destination.iterdir()):
            raise SkillInstallError("modified")
        planned = []
        for name, data in content.items():
            path = destination / name
            if (
                path.is_symlink()
                or path.parent.is_symlink()
                or (path.exists() and not path.is_file())
            ):
                raise SkillInstallError("path")
            old = path.read_bytes() if path.is_file() else None
            if (
                old is not None
                and old != data
                and hashlib.sha256(old).hexdigest() != expected.get(name)
            ):
                raise SkillInstallError("modified")
            planned.append(_InstallFile(path, old, data))
        # 해시 목록은 이 대상의 리소스가 모두 준비된 뒤에만 교체한다.
        planned.append(_InstallFile(marker, previous_marker, manifest))
        return planned

    def _apply(self, change: _InstallFile, rollback: ExitStack) -> None:
        """사전 검사 이후의 변경을 확인하고 실제 교체한 파일만 복구 대상으로 남긴다."""
        path = change.path
        if path.is_symlink() or (path.read_bytes() if path.exists() else None) != change.previous:
            raise SkillInstallError("modified")
        if change.previous == change.content and stat.S_IMODE(path.stat().st_mode) == 0o600:
            return
        missing = []
        directory = path.parent
        while directory != self._home and not directory.exists():
            missing.append(directory)
            directory = directory.parent
        for directory in reversed(missing):
            directory.mkdir(parents=True, mode=0o700)
            rollback.callback(_remove_empty, directory)
        write_private_file(path, change.content)
        rollback.callback(change.restore)

    def install(self, target: SkillTarget) -> SkillInstallResult:
        """선택한 대상 모두를 사전 검사하고 실패하면 교체 역순으로 정리한다."""
        if target == "both":
            selected = tuple(_TARGETS.values())
        elif target in _TARGETS:
            selected = (_TARGETS[target],)
        else:
            raise SkillInstallError("path")
        try:
            content = bundled_files()
            manifest = install_marker(content)
            planned = [
                change
                for _, root in selected
                for change in self._prepare(self._path(root), content, manifest)
            ]
            with ExitStack() as rollback:
                for change in planned:
                    self._apply(change, rollback)
                rollback.pop_all()
        except (OSError, ValueError):
            raise SkillInstallError("storage") from None
        return SkillInstallResult(tuple(name for name, _ in selected))
