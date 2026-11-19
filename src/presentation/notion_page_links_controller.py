"""페이지 링크 초안과 비동기 명시적 저장을 관리한다."""

from collections.abc import Callable, Mapping
from concurrent.futures import Future, ThreadPoolExecutor

from src.application.notion_page_links import (
    NotionPageLinksClient,
    NotionPageLinksState,
    PageLinksError,
)
from src.presentation.read_retry import ReadRetry


class NotionPageLinksController:
    """쓰기 실패에서 입력을 유지하고 조회만 자동 재시도한다."""

    def __init__(
        self, client: NotionPageLinksClient, apply_links: Callable[[NotionPageLinksState], None]
    ) -> None:
        """관리 API와 저장 후 표시 새로고침만 주입한다."""
        self._client, self._apply_links = client, apply_links
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="notion-page-links")
        self._pending: Future[NotionPageLinksState] | None = None
        self._saving = False
        self._closed = False
        self._read_retry = ReadRetry()
        self.state: NotionPageLinksState | None = None
        self.generation = 0
        self.dirty = False
        self.blocked = False
        self.error: str | None = None
        self.reload()

    @property
    def busy(self) -> bool:
        """조회와 저장을 겹치지 않는다."""
        return self._pending is not None

    @property
    def editable(self) -> bool:
        """확정 목록을 읽고 미확정 쓰기가 없을 때만 편집한다."""
        return (
            not self._closed
            and self.state is not None
            and not self.busy
            and not self.blocked
            and not self._read_retry.waiting
        )

    def reload(self) -> None:
        """명시적으로 다시 읽을 때만 초안을 버린다."""
        if self._closed or self.busy:
            return
        self._saving, self.dirty, self.blocked = False, False, False
        self.error = None
        self._read_retry.cancel()
        self._pending = self._executor.submit(self._client.read)

    def edited(self) -> None:
        """입력을 보존하고 자동 저장하지 않는다."""
        if self.editable:
            self.dirty, self.error = True, None

    def save(self, links: Mapping[str, str | None]) -> None:
        """전체 주소와 읽은 버전을 한 번만 제출한다."""
        if self._closed or not self.editable or not self.dirty or self.state is None:
            return
        self._saving, self.error = True, None
        self._pending = self._executor.submit(self._client.save, self.state.revision, links)

    def tick(self) -> None:
        """확정 결과만 반영하고 저장 성공에서만 표시를 갱신한다."""
        if self._closed:
            return
        if self._read_retry.due:
            self.reload()
        if self._pending is None or not self._pending.done():
            return
        future, self._pending = self._pending, None
        try:
            self.state = future.result()
            self.generation += 1
            self._read_retry.reset()
            self.dirty, self.error = False, None
            self._apply_links(self.state)
        except Exception as error:
            if self._saving:
                invalid = isinstance(error, PageLinksError) and error.code == "invalid"
                self.blocked = not invalid
                self.error = (
                    "Notion 페이지 주소를 확인하세요."
                    if invalid
                    else "다시 불러와 링크가 저장됐는지 확인하세요."
                )
            else:
                self.error = "잠시 후 페이지 링크를 다시 불러옵니다."
                self._read_retry.schedule()

    @property
    def message(self) -> str:
        """주소 변경 상태를 다른 설정 저장과 구분한다."""
        if self.busy:
            return "링크 저장 중…" if self._saving else "링크 불러오는 중…"
        return self.error or ("링크 수정 중" if self.dirty else "")

    def close(self) -> None:
        """추가 요청을 막고 작업자를 정리한다."""
        self._closed = True
        self._read_retry.cancel()
        self._executor.shutdown(wait=False, cancel_futures=True)
