"""설정의 앱 정보 페이지에 앱 아이콘·이름·버전과 업데이트 확인을 가운데 정렬로 둔다."""

from importlib.metadata import version

from AppKit import NSImage, NSImageView, NSScrollView, NSTextAlignmentCenter, NSView

from src.assets import APP_ICON
from src.presentation.native_views import frame, label
from src.presentation.update_button import UpdateChecks

# 설정 페이지 스크롤 영역의 폭이다.
PAGE_WIDTH = 584.0
ICON_SIZE = 128.0


class AppInfoView:
    """읽기 전용 정보만 보여 주고 저장 상태와는 연결하지 않는다."""

    def __init__(self, page: NSScrollView, viewport: float, updates: UpdateChecks | None) -> None:
        """아이콘·이름·버전·업데이트 버튼을 위에서부터 한 줄씩 가운데에 쌓는다."""
        document = NSView.alloc().initWithFrame_(frame(0, 0, PAGE_WIDTH, viewport))
        page.setDocumentView_(document)
        top = viewport - 48
        icon = NSImageView.alloc().initWithFrame_(
            frame((PAGE_WIDTH - ICON_SIZE) / 2, top - ICON_SIZE, ICON_SIZE, ICON_SIZE)
        )
        icon.setImage_(NSImage.alloc().initWithContentsOfFile_(str(APP_ICON)))
        document.addSubview_(icon)
        top -= ICON_SIZE + 12
        name = label(document, "Catch", 0, top - 30, PAGE_WIDTH, size=22, bold=True)
        name.setAlignment_(NSTextAlignmentCenter)
        # 개발 실행본은 배포 버전과 섞이지 않게 버전 옆에 함께 밝힌다.
        current = updates.current if updates is not None else version("maeng-mate")
        development = updates is not None and updates.actions.is_development
        details = label(
            document,
            f"버전 {current}" + (" · 개발용" if development else ""),
            0,
            top - 54,
            PAGE_WIDTH,
            size=12,
            secondary=True,
        )
        details.setAlignment_(NSTextAlignmentCenter)
        if updates is not None:
            updates.attach(document, (PAGE_WIDTH - 140) / 2, top - 96, 140)
