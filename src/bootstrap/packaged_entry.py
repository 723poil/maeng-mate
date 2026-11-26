"""Finder 실행과 내부 명령을 구분하는 동봉 앱 진입점이다."""

import multiprocessing
import sys


def main() -> None:
    """독립 프로세스에서 CLI를 유지하고 Finder 실패는 짧은 기본 대화상자로 알린다."""
    multiprocessing.freeze_support()
    if sys.argv[1:] == ["--check"]:
        from src.bootstrap.packaging_check import check_bundle

        check_bundle()
        return
    if len(sys.argv) > 1:
        from src.bootstrap.cli import main as command_main

        command_main()
        return
    from src.bootstrap.environment import ConfigError

    try:
        # 서비스 모듈을 불러올 때 발생하는 번들 프로필 오류도 Finder에 표시한다.
        from src.bootstrap.service import run_service

        run_service("finder")
    except Exception as error:
        from AppKit import NSAlert, NSApplication, NSApplicationActivationPolicyAccessory

        app = NSApplication.sharedApplication()
        app.setActivationPolicy_(NSApplicationActivationPolicyAccessory)
        alert = NSAlert.alloc().init()
        alert.setMessageText_("Catch를 실행하지 못했습니다.")
        alert.setInformativeText_(
            str(error)
            if isinstance(error, ConfigError)
            else "앱을 다시 실행하거나 로컬 설치 상태를 확인하세요."
        )
        alert.addButtonWithTitle_("확인")
        alert.runModal()
        sys.exit(1)


if __name__ == "__main__":
    main()
