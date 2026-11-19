"""사용자가 고른 이미지를 알림 앱에 쓸 작은 PNG·ICNS로 변환한다."""

import struct
from pathlib import Path

from AppKit import (
    NSBitmapImageRep,
    NSCalibratedRGBColorSpace,
    NSCompositingOperationSourceOver,
    NSGraphicsContext,
    NSImage,
    NSPNGFileType,
)

from src.application.notification_identity import validate_identity
from src.assets import APP_ICON
from src.domain.notification_identity import NotificationIdentity


def icon_png(path: str | None = None) -> bytes:
    """파일 크기·이미지 크기를 제한하고 원본 경로 대신 256px PNG를 반환한다."""
    file = Path(path) if path is not None else APP_ICON
    # 앱에 동봉한 벡터 원본만 예외로 읽고 사용자 파일의 형식·크기 제한은 유지한다.
    if path is not None and (
        file.suffix.lower() not in {".png", ".jpg", ".jpeg", ".icns"}
        or file.stat().st_size > 8 * 1024 * 1024
    ):
        raise ValueError("8MB 이하의 PNG·JPEG·ICNS 이미지를 선택하세요.")
    source = NSImage.alloc().initWithContentsOfFile_(str(file))
    if source is None or not source.isValid():
        raise ValueError("이미지를 읽지 못했습니다.")
    width, height = source.size()
    if not 0 < width <= 8192 or not 0 < height <= 8192:
        raise ValueError("이미지 크기를 확인하세요.")
    bitmap = NSBitmapImageRep.alloc().initWithBitmapDataPlanes_pixelsWide_pixelsHigh_bitsPerSample_samplesPerPixel_hasAlpha_isPlanar_colorSpaceName_bytesPerRow_bitsPerPixel_(
        None, 256, 256, 8, 4, True, False, NSCalibratedRGBColorSpace, 0, 0
    )
    context = NSGraphicsContext.graphicsContextWithBitmapImageRep_(bitmap)
    NSGraphicsContext.saveGraphicsState()
    NSGraphicsContext.setCurrentContext_(context)
    target: tuple[tuple[float, float], tuple[float, float]]
    try:
        scale = min(256 / width, 256 / height)
        size = (width * scale, height * scale)
        target = (((256 - size[0]) / 2, (256 - size[1]) / 2), size)
        source.drawInRect_fromRect_operation_fraction_(
            target, ((0, 0), (width, height)), NSCompositingOperationSourceOver, 1.0
        )
    finally:
        NSGraphicsContext.restoreGraphicsState()
    data = bitmap.representationUsingType_properties_(NSPNGFileType, {})
    if data is None:
        raise ValueError("아이콘을 변환하지 못했습니다.")
    png = bytes(data)
    validate_identity(NotificationIdentity(icon_png=png))
    return png


def icon_icns(png: bytes) -> bytes:
    """표준 256px PNG 표현을 ICNS 컨테이너에 넣는다."""
    validate_identity(NotificationIdentity(icon_png=png))
    block = b"ic08" + struct.pack(">I", len(png) + 8) + png
    return b"icns" + struct.pack(">I", len(block) + 8) + block
