# 실행 코드와 공개 스킬 리소스만 포함하며 로컬 설정·DB·로그를 수집하지 않는다.
import os
from pathlib import Path
from PyInstaller.utils.hooks import collect_submodules

root = Path(SPECPATH).parent
assets = root / 'build/distribution-assets'
a = Analysis(
    [str(root / 'src/bootstrap/packaged_entry.py')],
    pathex=[str(root)],
    binaries=[],
    datas=[
        (str(root / 'src/assets/app-icon.svg'), 'src/assets'),
        (str(root / 'src/presentation/assets/MenuMark.svg'), 'src/presentation/assets'),
        (str(root / 'src/infrastructure/skill_resources'), 'src/infrastructure/skill_resources'),
        (str(assets / 'build-info.json'), '.'),
    ],
    hiddenimports=collect_submodules('src') + collect_submodules('uvicorn'),
    hookspath=[], hooksconfig={}, runtime_hooks=[], excludes=['pytest', 'mypy', 'ruff'], noarchive=False,
)
# 버전 조회로 자동 수집된 메타데이터 중 설치 원본 경로·캐시 정보는 배포하지 않는다.
a.datas = [item for item in a.datas
           if not Path(item[0]).match('maeng_mate-*.dist-info/*') or Path(item[0]).name == 'METADATA']
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name='MaengMate', debug=False,
          bootloader_ignore_signals=False, strip=False, upx=False, console=False,
          argv_emulation=False, target_arch=None, codesign_identity=None)
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name='MaengMate')
app = BUNDLE(coll, name='Maeng Mate.app', icon=str(assets / 'MaengMate.icns'),
    bundle_identifier='local.maeng-mate', version=os.environ['MAENG_MATE_BUILD_VERSION'],
    info_plist={'CFBundleName': 'Maeng Mate', 'CFBundleDisplayName': 'Maeng Mate',
                'CFBundleVersion': os.environ['MAENG_MATE_BUILD_VERSION'],
                **({'SUPublicEDKey': os.environ['SPARKLE_PUBLIC_ED_KEY'],
                    'SUFeedURL': os.environ['SPARKLE_FEED_URL'],
                    'SUEnableAutomaticChecks': False, 'SUAllowsAutomaticUpdates': False,
                    'SUVerifyUpdateBeforeExtraction': True, 'SURequireSignedFeed': True}
                   if 'SPARKLE_PUBLIC_ED_KEY' in os.environ else {}),
                'LSUIElement': True, 'LSMinimumSystemVersion': '26.0',
                'NSPrincipalClass': 'NSApplication', 'NSHighResolutionCapable': True})
