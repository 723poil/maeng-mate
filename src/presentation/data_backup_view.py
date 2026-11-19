"""로컬 백업을 내보내고 검증한 파일로 전체 복원을 시작하는 설정 페이지다."""

from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

from AppKit import (
    NSAlert,
    NSAlertSecondButtonReturn,
    NSAlertStyleWarning,
    NSModalResponseOK,
    NSOpenPanel,
    NSSavePanel,
    NSScrollView,
    NSView,
)
from Foundation import NSObject

from src.application.data_backup import BackupPreview, DataBackupActions, DataBackupError
from src.application.runtime_settings_service import RestartOperation
from src.presentation.native_views import button, card, frame, label


class BackupButtons(NSObject):
    """백업과 불러오기 버튼을 페이지의 파일 선택 동작에 연결한다."""

    owner: "DataBackupView"

    def saveBackup_(self, sender: object) -> None:
        """새 백업을 만들 위치를 선택한다."""
        self.owner.choose_export()

    def openBackup_(self, sender: object) -> None:
        """선택한 백업의 검증을 요청한다."""
        self.owner.choose_restore()


class DataBackupView:
    """시간이 걸리는 파일 검증은 작업자에 맡기고 Cocoa 입력은 메인 스레드에서 처리한다."""

    def __init__(
        self,
        page: NSScrollView,
        height: float,
        actions: DataBackupActions,
        pending_edits: Callable[[], bool],
    ) -> None:
        """기록과 설정의 복원 범위를 표시하고 파일 작업을 한 번에 하나씩 받는다."""
        self._actions = actions
        self._pending_edits = pending_edits
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="data-backup")
        self._export: Future[None] | None = None
        self._preview: tuple[Path, Future[BackupPreview]] | None = None
        self._restore: RestartOperation | None = None
        self._choosing = False
        self.message = ""
        document = NSView.alloc().initWithFrame_(frame(0, 0, 584, height))
        page.setDocumentView_(document)
        self._buttons = BackupButtons.alloc().init()
        self._buttons.owner = self
        card(document, 16, height - 157, 552, 149)
        label(document, "백업 파일", 28, height - 40, 390, size=13, bold=True)
        label(
            document,
            "출퇴근 기록, 수집한 업무 데이터, 앱 설정과 근무 규칙을 저장합니다.",
            28,
            height - 68,
            520,
        )
        label(
            document,
            "Notion·Slack 인증 정보와 MCP 접근 권한은 이 Mac에서 관리합니다.",
            28,
            height - 91,
            520,
            secondary=True,
        )
        self._save = button(
            document, "백업하기…", 28, height - 137, 108, self._buttons, "saveBackup:"
        )
        self._open = button(
            document, "백업 불러오기…", 146, height - 137, 124, self._buttons, "openBackup:"
        )

    @property
    def _busy(self) -> bool:
        """패널이나 확인 대화상자의 중첩 이벤트에서도 동작 중복을 막는다."""
        return self._choosing or any(
            operation is not None for operation in (self._export, self._preview, self._restore)
        )

    def choose_export(self) -> None:
        """새 이름을 고른 경우에만 파일 작성을 요청한다."""
        if self._busy:
            return
        self._choosing = True
        try:
            panel = NSSavePanel.savePanel()
            panel.setAllowedFileTypes_(["maengbackup"])
            panel.setNameFieldStringValue_(
                datetime.now().strftime("MaengMate-%Y%m%d-%H%M%S.maengbackup")
            )
            panel.setMessage_("백업을 저장할 위치와 새 파일 이름을 선택하세요.")
            if panel.runModal() != NSModalResponseOK:
                return
            url = panel.URL()
            if url is not None:
                self._export = self._executor.submit(self._actions.export, Path(url.path()))
                self.message = "백업 파일을 저장하는 중…"
        finally:
            self._choosing = False
            self._update_buttons()

    def choose_restore(self) -> None:
        """파일 선택을 취소하면 작업자나 복원 요청을 만들지 않는다."""
        if self._busy:
            return
        self._choosing = True
        try:
            panel = NSOpenPanel.openPanel()
            panel.setCanChooseDirectories_(False)
            panel.setAllowsMultipleSelection_(False)
            panel.setAllowedFileTypes_(["maengbackup"])
            panel.setPrompt_("백업 확인")
            if panel.runModal() != NSModalResponseOK:
                return
            url = panel.URL()
            if url is not None:
                path = Path(url.path())
                self._preview = path, self._executor.submit(self._actions.preview, path)
                self.message = "백업 파일을 확인하는 중…"
        finally:
            self._choosing = False
            self._update_buttons()

    def _confirm_restore(self, path: Path, preview: BackupPreview) -> None:
        """백업 시각과 변경 범위를 알린 뒤 명시적으로 확인한 파일만 전달한다."""
        if self._pending_edits():
            self.message = "진행 중인 작업과 편집을 마친 뒤 다시 불러오세요."
            return
        stamp = datetime.fromisoformat(preview.stamp).astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")
        dialog = NSAlert.alloc().init()
        dialog.setAlertStyle_(NSAlertStyleWarning)
        dialog.setMessageText_("이 백업으로 전체 복원할까요?")
        dialog.setInformativeText_(
            f"백업 시각  {stamp}\n\n현재 출퇴근 기록, 수집한 업무 데이터, 앱 설정과 근무 규칙을 "
            "이 시점의 데이터로 교체합니다. 복원 직전 상태는 별도 백업으로 보관하고 앱을 다시 시작합니다."
        )
        dialog.addButtonWithTitle_("취소")
        dialog.addButtonWithTitle_("전체 복원")
        if dialog.runModal() == NSAlertSecondButtonReturn:
            self._restore = self._actions.restore(path, preview)
            self.message = "전체 복원 중입니다. 앱이 다시 시작됩니다."
        else:
            self.message = "복원을 취소했습니다."

    def tick(self, *, visible: bool) -> None:
        """완료된 작업을 회수하고 현재 페이지를 보는 동안에만 복원을 확인한다."""
        if not self._busy or self._choosing:
            return
        try:
            if self._export is not None and self._export.done():
                future, self._export = self._export, None
                future.result()
                self.message = "백업 파일을 저장했습니다."
            if self._preview is not None and self._preview[1].done():
                (path, inspected), self._preview = self._preview, None
                preview = inspected.result()
                if visible:
                    # 대화상자에서 메인 루프가 재진입해도 새 파일 작업을 시작할 수 없다.
                    self._choosing = True
                    try:
                        self._confirm_restore(path, preview)
                    finally:
                        self._choosing = False
                else:
                    self.message = "복원을 시작하려면 백업을 다시 선택하세요."
            if self._restore is not None and (exit_code := self._restore.poll()) is not None:
                self._restore = None
                self.message = (
                    "복원 작업을 마쳤습니다."
                    if exit_code == 0
                    else "전체 복원을 완료하지 못했습니다."
                )
        except DataBackupError as error:
            self.message = str(error)
        except FileExistsError:
            self.message = "같은 이름의 파일이 있습니다. 새 이름으로 백업하세요."
        except Exception:
            self.message = "백업 작업을 완료하지 못했습니다. 파일과 접근 권한을 확인하세요."
        self._update_buttons()

    def _update_buttons(self) -> None:
        """현재 동작이 끝나기 전에는 재요청을 받지 않는다."""
        for control in (self._save, self._open):
            control.setEnabled_(not self._busy)

    def close(self) -> None:
        """앱 종료 때 진행 중인 파일 작성은 끝까지 마치고 대기 작업을 정리한다."""
        self._executor.shutdown(wait=True, cancel_futures=True)
