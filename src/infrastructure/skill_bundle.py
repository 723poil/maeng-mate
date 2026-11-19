"""동봉 스킬 파일과 이전 설치 버전을 식별하는 해시 목록을 관리한다."""

import hashlib
import json
import re
from importlib.resources import files

from src.application.ai_skills import SkillInstallError, version_numbers

SKILL_FILES = ("SKILL.md", "agents/openai.yaml")
SKILL_VERSION = "1.0.0"
MARKER = ".maeng-mate-install.json"


def bundled_files() -> dict[str, bytes]:
    """설치에 필요한 파일을 모두 읽고 비어 있는 리소스는 거부한다."""
    root = files("src.infrastructure").joinpath("skill_resources", "maeng-mate")
    content = {name: root.joinpath(name).read_bytes() for name in SKILL_FILES}
    if not all(content.values()):
        raise SkillInstallError("storage")
    return content


def install_marker(content: dict[str, bytes]) -> bytes:
    """설치 기록 형식과 스킬 배포 버전을 각각 기록한다."""
    hashes = {name: hashlib.sha256(data).hexdigest() for name, data in content.items()}
    return json.dumps({"version": 2, "skillVersion": SKILL_VERSION, "files": hashes}).encode()


def installed_manifest(marker: bytes) -> tuple[str | None, dict[str, str]]:
    """이전 형식은 버전 미기록으로 읽고 두 형식 모두 기존 파일 해시를 검증한다."""
    try:
        raw: object = json.loads(marker)
    except ValueError:
        raise SkillInstallError("modified") from None
    if (
        not isinstance(raw, dict)
        or type(raw.get("version")) is not int
        or not isinstance(raw.get("files"), dict)
        or set(raw["files"]) != set(SKILL_FILES)
    ):
        raise SkillInstallError("modified")
    version = None
    if raw["version"] == 2 and set(raw) == {"version", "files", "skillVersion"}:
        value: object = raw["skillVersion"]
        if not isinstance(value, str) or not re.fullmatch(
            r"(0|[1-9][0-9]*)(\.(0|[1-9][0-9]*)){2}", value
        ):
            raise SkillInstallError("modified")
        version_numbers(value)
        version = value
    elif raw["version"] != 1 or set(raw) != {"version", "files"}:
        raise SkillInstallError("modified")
    hashes: dict[str, str] = {}
    for name in SKILL_FILES:
        digest = raw["files"][name]
        if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise SkillInstallError("modified")
        hashes[name] = digest
    return version, hashes
