"""개인 설정을 포함하지 않는 macOS 앱과 드래그 설치 DMG를 만든다."""

import hashlib
import json
import os
import platform
import shutil
import subprocess
import sys
import tempfile
import tomllib
from pathlib import Path

from sparkle_release import RELEASES, TOOLS, prepare_sparkle

ROOT = Path(__file__).resolve().parent.parent


def run(arguments: list[str], *, env: dict[str, str] | None = None) -> None:
    """실패한 빌드를 다음 단계로 넘기지 않고 명령 원문 로그를 제한한다."""
    result = subprocess.run(
        arguments, cwd=ROOT, env=env, capture_output=True, text=True, check=False
    )
    if result.returncode:
        # 배포 작업의 도구 출력에는 비밀 환경·개인 설정을 전달하지 않는다.
        print(result.stderr[-6000:], file=sys.stderr)
        raise SystemExit("빌드 실패: " + Path(arguments[0]).name)


def build_icon(assets: Path) -> Path:
    """앱과 설치 볼륨에서 공유할 일반·Retina 크기의 ICNS를 만든다."""
    from AppKit import NSBitmapImageRep, NSImage, NSPNGFileType

    image = NSImage.alloc().initWithContentsOfFile_(str(ROOT / "src/assets/app-icon.svg"))
    if image is None:
        raise SystemExit("배포 아이콘 원본을 읽지 못했습니다.")
    bitmap = NSBitmapImageRep.imageRepWithData_(image.TIFFRepresentation())
    master = assets / "app-icon.png"
    master.write_bytes(bytes(bitmap.representationUsingType_properties_(NSPNGFileType, {})))
    iconset = assets / "MaengMate.iconset"
    iconset.mkdir(exist_ok=True)
    for points in (16, 32, 128, 256, 512):
        for scale, suffix in ((1, ""), (2, "@2x")):
            pixels = str(points * scale)
            target = iconset / f"icon_{points}x{points}{suffix}.png"
            run(["/usr/bin/sips", "-z", pixels, pixels, str(master), "--out", str(target)])
    icon = assets / "MaengMate.icns"
    run(["/usr/bin/iconutil", "-c", "icns", str(iconset), "-o", str(icon)])
    return icon


def main() -> None:
    """빌드 도구는 개발 환경에서만 사용하고 산출물에는 포함하지 않는다."""
    if sys.platform != "darwin":
        raise SystemExit("macOS에서 빌드하세요.")
    os.chdir(ROOT)
    assets = ROOT / "build/distribution-assets"
    assets.mkdir(parents=True, exist_ok=True)
    public_key = os.environ.get("SPARKLE_PUBLIC_ED_KEY", "").strip()
    if public_key:
        prepare_sparkle(public_key)
    version = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["version"]
    sources = sorted((ROOT / "src").rglob("*.py")) + sorted(
        (ROOT / "src/infrastructure/skill_resources").rglob("*")
    )
    sources.extend(sorted((ROOT / "src/presentation/assets").glob("*.svg")))
    sources.append(ROOT / "src/assets/app-icon.svg")
    digest = hashlib.sha256()
    for source in sources:
        if source.is_file() and "__pycache__" not in source.parts:
            digest.update(str(source.relative_to(ROOT)).encode())
            digest.update(source.read_bytes())
    metadata = {
        "build": digest.hexdigest(),
        "version": version,
        "architecture": platform.machine(),
        "minimumMacOS": "26.0",
    }
    (assets / "build-info.json").write_text(json.dumps(metadata, sort_keys=True))
    icon = build_icon(assets)
    environment = {
        key: os.environ[key] for key in ("PATH", "HOME", "LANG", "TMPDIR") if key in os.environ
    }
    environment["PYINSTALLER_CONFIG_DIR"] = str(ROOT / ".cache/pyinstaller")
    environment["MAENG_MATE_BUILD_VERSION"] = version
    if public_key:
        environment["SPARKLE_PUBLIC_ED_KEY"] = public_key
        environment["SPARKLE_FEED_URL"] = RELEASES + "/latest/download/appcast.xml"
    dist = ROOT / "dist"
    build = ROOT / "build/distribution"
    # 중간 빌드에도 같은 발신자 ID의 앱이 남지 않도록 임시 위치에서 압축한다.
    archive = assets / "NotificationAgent.zip"
    with tempfile.TemporaryDirectory() as directory:
        notification_dist = Path(directory)
        print("알림 앱 빌드 중…", flush=True)
        run(
            [
                sys.executable,
                "-m",
                "PyInstaller",
                "--noconfirm",
                "--distpath",
                str(notification_dist),
                "--workpath",
                str(build / "notification-work"),
                str(ROOT / "packaging/notification.spec"),
            ],
            env=environment,
        )
        run(
            [
                "/usr/bin/ditto",
                "-c",
                "-k",
                str(notification_dist / "NotificationAgent.app"),
                str(archive),
            ]
        )
    print("Maeng Mate 앱 빌드 중…", flush=True)
    run(
        [
            sys.executable,
            "-m",
            "PyInstaller",
            "--noconfirm",
            "--distpath",
            str(dist),
            "--workpath",
            str(build / "app-work"),
            str(ROOT / "packaging/app.spec"),
        ],
        env=environment,
    )
    app = dist / "Maeng Mate.app"
    if public_key:
        shutil.copytree(
            TOOLS / "Sparkle.framework",
            app / "Contents/Frameworks/Sparkle.framework",
            symlinks=True,
        )
        shutil.copyfile(TOOLS / "LICENSE", app / "Contents/Resources/Sparkle-LICENSE")
    shutil.copyfile(archive, app / "Contents/Resources/NotificationAgent.zip")
    run(["/usr/bin/codesign", "--force", "--deep", "--sign", "-", str(app)])
    run(["/usr/bin/codesign", "--verify", "--deep", "--strict", str(app)])
    print("동봉 실행·리소스 확인 중…", flush=True)
    executable = app / "Contents/MacOS/MaengMate"
    result = subprocess.run(
        [str(executable), "--check"],
        capture_output=True,
        text=True,
        timeout=90,
        env={**environment, "PYINSTALLER_RESET_ENVIRONMENT": "1"},
    )
    if result.returncode:
        print(result.stderr[-6000:], file=sys.stderr)
        raise SystemExit("동봉 실행 확인 실패")
    try:
        checked = json.loads(result.stdout)
    except ValueError:
        raise SystemExit("동봉 실행 결과 확인 실패") from None
    if not isinstance(checked, dict) or checked.get("ok") is not True:
        raise SystemExit("동봉 실행 결과 확인 실패")
    if checked.get("version") != version:
        raise SystemExit("앱에 표시할 버전과 배포 버전이 일치하지 않습니다.")
    stage = build / "dmg"
    if stage.exists():
        shutil.rmtree(stage)
    stage.mkdir(parents=True)
    shutil.copytree(app, stage / app.name, symlinks=True)
    (stage / "Applications").symlink_to("/Applications")
    # 읽기 전용 설치 볼륨도 Finder에서 앱과 같은 아이콘으로 식별한다.
    shutil.copyfile(icon, stage / ".VolumeIcon.icns")
    name = f"Maeng-Mate-{version}-macOS-{platform.machine()}.dmg"
    destination = dist / name
    writable = build / "installer.dmg"
    print("DMG 만들기…", flush=True)
    run(
        [
            "/usr/bin/hdiutil",
            "create",
            "-volname",
            "Maeng Mate",
            "-srcfolder",
            str(stage),
            "-format",
            "UDRW",
            "-ov",
            str(writable),
        ]
    )
    # 원본 폴더의 Finder 플래그는 볼륨 루트로 복사되지 않아 압축 전에 직접 지정한다.
    with tempfile.TemporaryDirectory() as mount:
        run(["/usr/bin/hdiutil", "attach", "-nobrowse", "-mountpoint", mount, str(writable)])
        try:
            run(["/usr/bin/SetFile", "-a", "C", mount])
        finally:
            run(["/usr/bin/hdiutil", "detach", mount])
    run(
        [
            "/usr/bin/hdiutil",
            "convert",
            str(writable),
            "-format",
            "UDZO",
            "-ov",
            "-o",
            str(destination),
        ]
    )
    (dist / (name + ".sha256")).write_text(
        hashlib.sha256(destination.read_bytes()).hexdigest() + "  " + name + "\n"
    )
    print("완료: dist/" + name)


if __name__ == "__main__":
    main()
