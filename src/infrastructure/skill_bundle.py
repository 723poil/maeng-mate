"""동봉 스킬 파일과 이전 설치 버전을 식별하는 해시 목록을 관리한다."""

import hashlib
import json
from importlib.resources import files

from src.application.ai_skills import SkillInstallError

SKILL_FILES = ("SKILL.md", "agents/openai.yaml")
MARKER = ".maeng-mate-install.json"


def bundled_files() -> dict[str, bytes]:
    """설치에 필요한 파일을 모두 읽고 비어 있는 리소스는 거부한다."""
    root = files("src.infrastructure").joinpath("skill_resources", "maeng-mate")
    content = {name: root.joinpath(name).read_bytes() for name in SKILL_FILES}
    if not all(content.values()):
        raise SkillInstallError("storage")
    return content


def install_marker(content: dict[str, bytes]) -> bytes:
    """기존 설치와 호환되는 버전 1 해시 목록을 만든다."""
    hashes = {name: hashlib.sha256(data).hexdigest() for name, data in content.items()}
    return json.dumps({"version": 1, "files": hashes}).encode()


def installed_hashes(marker: bytes) -> dict[str, str]:
    """지원하는 설치 버전과 파일 목록이 일치할 때만 덮어쓰기 근거로 사용한다."""
    raw: object = json.loads(marker)
    if (
        not isinstance(raw, dict)
        or set(raw) != {"version", "files"}
        or type(raw["version"]) is not int
        or raw["version"] != 1
        or not isinstance(raw["files"], dict)
        or set(raw["files"]) != set(SKILL_FILES)
    ):
        raise SkillInstallError("modified")
    hashes: dict[str, str] = {}
    for name in SKILL_FILES:
        digest = raw["files"][name]
        if not isinstance(digest, str):
            raise SkillInstallError("modified")
        hashes[name] = digest
    return hashes
