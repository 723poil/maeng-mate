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

from dmgbuild import build_dmg
from dmgbuild.core import DMGError
from sparkle_release import RELEASES, TOOLS, prepare_sparkle

ROOT = Path(__file__).resolve().parent.parent
CODESIGN = "/usr/bin/codesign"
# 단일·범용 Mach-O 파일의 앞 4바이트다. 서명할 실행 코드만 고르는 데 쓴다.
MACH_O_MAGIC = {
    bytes.fromhex(value) for value in ("cffaedfe", "cefaedfe", "feedfacf", "feedface", "cafebabe")
}
# 설치 창의 크기와 아이콘 중심의 위쪽 거리다. 배경 그림과 아이콘 배치가 같은 값을 쓴다.
DMG_WINDOW = (660, 400)
DMG_ICON_TOP = 170


def run(arguments: list[str], *, env: dict[str, str] | None = None) -> None:
    """실패한 빌드를 다음 단계로 넘기지 않고 명령 원문 로그를 제한한다."""
    result = subprocess.run(
        arguments, cwd=ROOT, env=env, capture_output=True, text=True, check=False
    )
    if result.returncode:
        # 배포 작업의 도구 출력에는 비밀 환경·개인 설정을 전달하지 않는다.
        print(result.stderr[-6000:], file=sys.stderr)
        raise SystemExit("빌드 실패: " + Path(arguments[0]).name)


def sign(bundle: Path, identity: str) -> None:
    """배포 인증서가 없으면 임시 서명을, 있으면 Apple 공증 조건에 맞는 서명을 적용한다."""
    if not identity:
        run([CODESIGN, "--force", "--deep", "--sign", "-", str(bundle)])
        return
    # 공증은 번들 안의 모든 실행 코드에 같은 인증서·강화 런타임·보안 타임스탬프를 요구한다.
    # Sparkle 내려받기 서비스처럼 구성요소에 원래 부여된 권한은 유지한다.
    options = [CODESIGN, "--force", "--options", "runtime", "--timestamp", "--sign", identity]
    nested = [*options, "--preserve-metadata=entitlements"]
    binaries: list[str] = []
    bundles: list[Path] = []
    # 심볼릭 링크는 따라가지 않아 같은 파일을 두 번 서명하거나 번들 밖을 건드리지 않는다.
    for directory, folders, names in os.walk(bundle):
        bundles.extend(
            Path(directory) / folder
            for folder in folders
            if Path(folder).suffix in {".app", ".framework", ".xpc"}
            and not (Path(directory) / folder).is_symlink()
        )
        for name in names:
            path = Path(directory) / name
            if not path.is_symlink():
                with path.open("rb") as stream:
                    if stream.read(4) in MACH_O_MAGIC:
                        binaries.append(str(path))
    # 바깥 서명이 안쪽 코드의 해시를 담으므로 실행 파일, 깊은 내부 번들, 앱 순서로 서명한다.
    if binaries:
        run([*nested, *binaries])
    for item in sorted(bundles, key=lambda path: len(path.parts), reverse=True):
        run([*nested, str(item)])
    run([*options, str(bundle)])


def notarize(image: Path, profile: str) -> None:
    """서명한 DMG를 Apple 공증에 제출하고 승인 표를 DMG에 붙인다."""
    result = subprocess.run(
        [
            "/usr/bin/xcrun",
            "notarytool",
            "submit",
            str(image),
            "--keychain-profile",
            profile,
            "--wait",
            "--output-format",
            "json",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    try:
        report: object = json.loads(result.stdout)
    except ValueError:
        report = None
    # 제출 명령은 심사에서 거부돼도 성공으로 끝날 수 있어 심사 결과를 직접 확인한다.
    if result.returncode or not isinstance(report, dict) or report.get("status") != "Accepted":
        print(result.stderr[-6000:], file=sys.stderr)
        if isinstance(report, dict):
            print(
                f"공증 상태: {report.get('status')} · 제출 ID: {report.get('id')}", file=sys.stderr
            )
        raise SystemExit("빌드 실패: Apple 공증")
    # 승인 표를 붙여 두면 인터넷 연결 없이 처음 여는 Mac에서도 Gatekeeper가 확인할 수 있다.
    run(["/usr/bin/xcrun", "stapler", "staple", str(image)])


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


def build_background(assets: Path) -> Path:
    """설치 창의 앱에서 응용 프로그램 폴더로 향하는 화살표 배경을 일반·Retina로 그린다."""
    from AppKit import (
        NSBezierPath,
        NSBitmapImageRep,
        NSCalibratedRGBColorSpace,
        NSColor,
        NSGradient,
        NSGraphicsContext,
        NSPNGFileType,
    )
    from Foundation import NSAffineTransform

    width, height = DMG_WINDOW
    for scale, suffix in ((1, ""), (2, "@2x")):
        bitmap = NSBitmapImageRep.alloc().initWithBitmapDataPlanes_pixelsWide_pixelsHigh_bitsPerSample_samplesPerPixel_hasAlpha_isPlanar_colorSpaceName_bytesPerRow_bitsPerPixel_(
            None, width * scale, height * scale, 8, 4, True, False, NSCalibratedRGBColorSpace, 0, 0
        )
        NSGraphicsContext.saveGraphicsState()
        NSGraphicsContext.setCurrentContext_(
            NSGraphicsContext.graphicsContextWithBitmapImageRep_(bitmap)
        )
        zoom = NSAffineTransform.transform()
        zoom.scaleBy_(scale)
        zoom.concat()
        # Finder의 항목 이름은 화면 모드에 따라 검정 또는 흰색이므로 둘 다 읽히는 중간 밝기를 쓴다.
        NSGradient.alloc().initWithStartingColor_endingColor_(
            NSColor.colorWithSRGBRed_green_blue_alpha_(0.43, 0.51, 0.57, 1),
            NSColor.colorWithSRGBRed_green_blue_alpha_(0.37, 0.45, 0.51, 1),
        ).drawInRect_angle_(((0, 0), (width, height)), 270)
        # 아이콘 중심 높이에 맞춰 두 아이콘 사이의 빈 공간에만 그린다.
        center = height - DMG_ICON_TOP
        arrow = NSBezierPath.bezierPath()
        arrow.moveToPoint_((284, center))
        arrow.lineToPoint_((372, center))
        arrow.moveToPoint_((352, center + 20))
        arrow.lineToPoint_((374, center))
        arrow.lineToPoint_((352, center - 20))
        arrow.setLineWidth_(6)
        arrow.setLineCapStyle_(1)
        arrow.setLineJoinStyle_(1)
        NSColor.colorWithSRGBRed_green_blue_alpha_(1, 1, 1, 0.75).setStroke()
        arrow.stroke()
        NSGraphicsContext.restoreGraphicsState()
        target = assets / f"dmg-background{suffix}.png"
        target.write_bytes(bytes(bitmap.representationUsingType_properties_(NSPNGFileType, {})))
    return assets / "dmg-background.png"


def main() -> None:
    """빌드 도구는 개발 환경에서만 사용하고 산출물에는 포함하지 않는다."""
    if sys.platform != "darwin":
        raise SystemExit("macOS에서 빌드하세요.")
    os.chdir(ROOT)
    assets = ROOT / "build/distribution-assets"
    assets.mkdir(parents=True, exist_ok=True)
    public_key = os.environ.get("SPARKLE_PUBLIC_ED_KEY", "").strip()
    # 키체인에 있는 Developer ID Application 인증서의 이름이다. 비우면 임시 서명으로 빌드한다.
    identity = os.environ.get("CODESIGN_IDENTITY", "").strip()
    # notarytool store-credentials로 키체인에 저장한 공증 자격 정보의 프로필 이름이다.
    notary = os.environ.get("NOTARY_PROFILE", "").strip()
    if notary and not identity:
        raise SystemExit("공증에는 CODESIGN_IDENTITY의 Developer ID 서명이 필요합니다.")
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
        if identity:
            # 공증은 앱에 동봉한 압축 파일 안의 실행 코드도 검사한다.
            sign(notification_dist / "NotificationAgent.app", identity)
        run(
            [
                "/usr/bin/ditto",
                "-c",
                "-k",
                str(notification_dist / "NotificationAgent.app"),
                str(archive),
            ]
        )
    print("Catch 앱 빌드 중…", flush=True)
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
    app = dist / "Catch.app"
    if public_key:
        shutil.copytree(
            TOOLS / "Sparkle.framework",
            app / "Contents/Frameworks/Sparkle.framework",
            symlinks=True,
        )
        shutil.copyfile(TOOLS / "LICENSE", app / "Contents/Resources/Sparkle-LICENSE")
    shutil.copyfile(archive, app / "Contents/Resources/NotificationAgent.zip")
    sign(app, identity)
    run([CODESIGN, "--verify", "--deep", "--strict", str(app)])
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
    name = f"Catch-{version}-macOS-{platform.machine()}.dmg"
    destination = dist / name
    print("DMG 만들기…", flush=True)
    # Finder를 조작하지 않고 창 크기·아이콘 배치·배경을 볼륨에 기록해 CI에서도 같은 결과를 만든다.
    try:
        build_dmg(
            str(destination),
            "Catch",
            settings={
                "format": "UDZO",
                "files": [str(app)],
                "symlinks": {"Applications": "/Applications"},
                # 읽기 전용 설치 볼륨도 Finder에서 앱과 같은 아이콘으로 식별한다.
                "icon": str(icon),
                "background": str(build_background(assets)),
                "window_rect": ((200, 200), DMG_WINDOW),
                "default_view": "icon-view",
                "icon_size": 128,
                "text_size": 13,
                "icon_locations": {
                    app.name: (170, DMG_ICON_TOP),
                    "Applications": (490, DMG_ICON_TOP),
                },
            },
        )
    except DMGError as error:
        print(error.message[-6000:], file=sys.stderr)
        raise SystemExit("빌드 실패: DMG") from None
    if identity:
        # 체크섬과 업데이트 피드는 서명까지 끝난 DMG 바이트로 계산한다.
        run([CODESIGN, "--force", "--timestamp", "--sign", identity, str(destination)])
    if notary:
        print("Apple 공증 대기 중…", flush=True)
        notarize(destination, notary)
    (dist / (name + ".sha256")).write_text(
        hashlib.sha256(destination.read_bytes()).hexdigest() + "  " + name + "\n"
    )
    print("완료: dist/" + name)


if __name__ == "__main__":
    main()
