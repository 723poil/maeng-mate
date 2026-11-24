import os
# 알림 요청만 처리하는 독립 앱이며 수집·DB·MCP 모듈은 포함하지 않는다.
from pathlib import Path
from src.bootstrap.build_profile import RELEASE, DEVELOPMENT

from src.infrastructure.macos_icon import icon_icns, icon_png

# 사용자 설정을 반영하기 전의 독립 발신 앱도 같은 기본 아이콘으로 등록한다.
icon = Path(workpath) / "MaengMate.icns"
icon.write_bytes(icon_icns(icon_png()))
entry = str(Path(SPECPATH).parent / 'src/infrastructure/notification_app/agent.py')
profile = RELEASE if os.environ.get('SPARKLE_PUBLIC_ED_KEY') else DEVELOPMENT
a = Analysis([entry], pathex=[str(Path(SPECPATH).parent)], binaries=[], datas=[], hiddenimports=[], hookspath=[], hooksconfig={}, runtime_hooks=[], excludes=[], noarchive=False)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name='NotificationAgent', debug=False, bootloader_ignore_signals=False, strip=False, upx=False, console=False, argv_emulation=False)
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name='NotificationAgent')
app = BUNDLE(coll, name='NotificationAgent.app', icon=str(icon), bundle_identifier=profile.identifier + ".notifications", version='1.0', info_plist={'CFBundleName':'Catch','CFBundleDisplayName':'Catch','LSUIElement':True,'NSPrincipalClass':'NSApplication','NSAppleScriptEnabled':False})
