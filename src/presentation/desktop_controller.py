"""네이티브 UI와 단일 네트워크 작업자 사이의 상태를 관리한다."""

from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import replace

from src.application.day_ports import DayConflictError, DayInputError
from src.application.desktop import DayUpdate, DayView, DesktopClient, DesktopSnapshot
from src.application.errors import StatusReadError


class DesktopController:
    """작업자는 값만 반환하고 GUI 갱신은 poll을 호출한 메인 루프에서 한다."""

    def __init__(self, client: DesktopClient) -> None:
        """구체 HTTP 구현 대신 주입한 로컬 연결 계약만 사용한다."""
        self._client = client
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="menubar-api")
        self._pending: Future[DesktopSnapshot] | Future[DayView] | None = None
        self._closed = False
        self.snapshot: DesktopSnapshot | None = None
        self.error: str | None = None
        self.connected = False
        self.conflict = False
        self.draft: DayUpdate | None = None
        self.operation = ""
        self._page_links_update: tuple[int, tuple[str | None, str | None, str | None]] | None = None

    def update_page_links(
        self, revision: int, pages: tuple[str | None, str | None, str | None]
    ) -> None:
        """근무 입력·오류를 건드리지 않고 확정된 이동 주소만 즉시 반영한다."""
        if self.snapshot is not None and self.snapshot.page_links_revision > revision:
            return
        self._page_links_update = (revision, pages)
        if self.snapshot is not None:
            # 이 버전은 표시용 덮어쓰기다. 진행 중 조회가 확인해 줄 때까지 유지한다.
            self.snapshot = replace(
                self.snapshot,
                task_page=pages[0],
                qa_page=pages[1],
                meeting_page=pages[2],
                page_links_revision=revision,
            )

    def _with_page_links(self, snapshot: DesktopSnapshot) -> DesktopSnapshot:
        """저장 전에 시작한 늦은 조회가 주소를 되돌리지 않게 한다."""
        update = self._page_links_update
        if update is None:
            return snapshot
        revision, pages = update
        if snapshot.page_links_revision >= revision:
            self._page_links_update = None
            return snapshot
        return replace(
            snapshot,
            task_page=pages[0],
            qa_page=pages[1],
            meeting_page=pages[2],
            page_links_revision=revision,
        )

    @property
    def busy(self) -> bool:
        """완료 결과를 메인 스레드에서 반영할 때까지 새 요청을 막는다."""
        return self._pending is not None

    @property
    def can_edit(self) -> bool:
        """연결·버전이 확인된 화면에서만 수정한다."""
        return (
            self.connected
            and self.snapshot is not None
            and not self.busy
            and not self.conflict
            and not self._closed
        )

    def refresh(self) -> bool:
        """이미 요청이 있으면 다음 조회를 큐에 쌓지 않는다."""
        if self.busy or self._closed:
            return False
        self.operation = "fetch"
        self._pending = self._executor.submit(self._client.fetch)
        return True

    def set_interval(self, seconds: int) -> bool:
        """다른 조회·저장과 겹치지 않게 주기 저장을 비동기로 요청한다."""
        if self.busy or self._closed:
            return False
        self.operation = "interval"
        self._pending = self._executor.submit(self._client.set_interval, seconds)
        return True

    def save(self, update: DayUpdate) -> bool:
        """확인한 날짜·버전과 초안을 유지하고 서버 저장 결과를 기다린다."""
        if not self.can_edit:
            return False
        self.draft = update
        self.operation = "save"
        self._pending = self._executor.submit(self._client.save, update)
        return True

    def poll(self) -> bool:
        """작업 결과만 회수하며 실패를 성공 화면이나 빈 기록으로 바꾸지 않는다."""
        future = self._pending
        if future is None or not future.done() or self._closed:
            return False
        self._pending = None
        try:
            result = future.result()
            if isinstance(result, DayView):
                if self.snapshot is None:
                    raise RuntimeError
                self.snapshot = replace(self.snapshot, day=result)
                self.draft = None
            else:
                self.snapshot = self._with_page_links(result)
            self.connected = True
            self.conflict = False
            self.error = None
        except DayConflictError as error:
            self.conflict = True
            self.error = str(error)
        except DayInputError as error:
            self.error = str(error)
        except StatusReadError as error:
            self.connected = False
            self.error = str(error)
        except Exception:
            self.connected = False
            self.error = "상태를 확인하지 못했습니다. 로컬 앱 연결을 확인하세요."
        return True

    def close(self) -> None:
        """추가 요청을 차단하고 대기 중 작업을 취소한다."""
        self._closed = True
        self._executor.shutdown(wait=False, cancel_futures=True)
