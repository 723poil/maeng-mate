"""버전과 해시가 고정된 Sparkle을 준비하고 최종 DMG의 서명된 피드를 만든다."""

import base64
import hashlib
import os
import plistlib
import shutil
import subprocess
import tempfile
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
VERSION = "2.10.1"
DIGEST = "1a856d2a9edb8e628de88121d7be04cf0e2167f8641650b78fca00b14244cf27"
CACHE = ROOT / ".cache/sparkle"
TOOLS = ROOT / "build/distribution-assets/sparkle"
RELEASES = "https://github.com/723poil/maeng-mate/releases"
NAMESPACE = "{http://www.andymatuschak.org/xml-namespaces/sparkle}"


def prepare_sparkle(public_key: str) -> None:
    """공개키를 검사하고 검증된 공식 아카이브에서 도구와 번들을 꺼낸다."""
    if len(base64.b64decode(public_key, validate=True)) != 32:
        raise SystemExit("Sparkle Ed25519 공개키를 확인하세요.")
    name = f"Sparkle-{VERSION}.tar.xz"
    CACHE.mkdir(parents=True, exist_ok=True)
    archive = CACHE / name
    # 중단된 다운로드나 손상된 캐시를 다음 빌드가 그대로 사용하지 않게 한다.
    valid = False
    if archive.is_file():
        with archive.open("rb") as stream:
            valid = hashlib.file_digest(stream, "sha256").hexdigest() == DIGEST
    if not valid:
        address = f"https://github.com/sparkle-project/Sparkle/releases/download/{VERSION}/{name}"
        with tempfile.TemporaryDirectory(dir=CACHE) as directory:
            staged = Path(directory) / name
            with (
                urllib.request.urlopen(address, timeout=60) as response,
                staged.open("wb") as output,
            ):
                shutil.copyfileobj(response, output)
            with staged.open("rb") as stream:
                if hashlib.file_digest(stream, "sha256").hexdigest() != DIGEST:
                    raise SystemExit("Sparkle 다운로드 해시가 일치하지 않습니다.")
            staged.replace(archive)
    if TOOLS.exists():
        shutil.rmtree(TOOLS)
    TOOLS.mkdir(parents=True)
    subprocess.run(
        ["/usr/bin/tar", "-xf", archive, "-C", TOOLS, "./Sparkle.framework", "./bin", "./LICENSE"],
        check=True,
    )


def publish_feed() -> None:
    """이번 빌드 한 건의 DMG·공개키·한국어 노트를 대조하고 서명된 피드를 만든다."""
    dist = ROOT / "dist"
    info = plistlib.loads((dist / "Maeng Mate.app/Contents/Info.plist").read_bytes())
    version = info["CFBundleVersion"]
    dmg = dist / f"Maeng-Mate-{version}-macOS-arm64.dmg"
    # 개인키는 표준 입력으로만 도구에 주며 자식 환경이나 인수에 복사하지 않는다.
    key = os.environ.pop("SPARKLE_PRIVATE_ED_KEY").strip()
    if not key or not info.get("SUPublicEDKey"):
        raise SystemExit("릴리스의 업데이트 서명 키를 설정하세요.")
    prefix = f"{RELEASES}/download/v{version}/"
    with tempfile.TemporaryDirectory(dir=dist) as directory:
        stage = Path(directory)
        os.link(dmg, stage / dmg.name)
        shutil.copyfile(ROOT / f"docs/releases/{version}.md", stage / dmg.with_suffix(".md").name)
        subprocess.run(
            [
                TOOLS / "bin/generate_appcast",
                "--ed-key-file",
                "-",
                "--maximum-deltas",
                "0",
                "--download-url-prefix",
                prefix,
                "--embed-release-notes",
                stage,
            ],
            input=key,
            text=True,
            check=True,
        )
        feed = stage / "appcast.xml"
        item = ET.parse(feed).find("./channel/item")
        enclosure = item.find("enclosure") if item is not None else None
        if (
            item is None
            or item.findtext(NAMESPACE + "version") != version
            or enclosure is None
            or enclosure.get("url") != prefix + dmg.name
            or enclosure.get("length") != str(dmg.stat().st_size)
            or not enclosure.get(NAMESPACE + "edSignature")
        ):
            raise SystemExit("배포 DMG와 업데이트 피드를 대조하지 못했습니다.")
        subprocess.run(
            [TOOLS / "bin/sign_update", "--ed-key-file", "-", "--verify", feed],
            input=key,
            text=True,
            check=True,
        )
        feed.replace(dist / "appcast.xml")


if __name__ == "__main__":
    publish_feed()
