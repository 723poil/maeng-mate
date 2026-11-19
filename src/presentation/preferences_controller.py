"""설정의 비동기 저장과 빠른 연속 선택의 최신 초안을 관리한다."""

import time
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import replace

from src.application.preferences import PreferencesClient
from src.domain.preferences import Preferences
from src.presentation.read_retry import ReadRetry


class PreferencesController:
    """늦은 저장 응답은 버전만 반영하고 새 선택을 되돌리지 않는다."""

    def __init__(self, client: PreferencesClient) -> None:
        """구체 저장 구현 대신 설정 클라이언트를 받는다."""
        self._client = client
        self._closed = False
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="preferences")
        self._pending: Future[Preferences] | None = None
        self._submitted: Preferences | None = None
        self.value: Preferences | None = None
        self.confirmed: Preferences | None = None
        self.error: str | None = None
        self.dirty = False
        self._edited = 0.0
        self._read_retry = ReadRetry()
        self.reload()

    def reload(self) -> None:
        """수동 다시 읽기는 재시도 대기를 건너뛰고 확정값을 조회한다."""
        if self._closed or self._pending is not None:
            return
        self._read_retry.reset()
        self._start_read()

    def _start_read(self) -> None:
        """저장을 동반하지 않는 설정 조회만 백그라운드에서 시작한다."""
        self._read_retry.cancel()
        self.error = None
        self.dirty = False
        self._submitted = None
        self._pending = self._executor.submit(self._client.read)

    def change(self, value: Preferences) -> None:
        """화면 선택을 즉시 보존하고 짧은 연속 입력을 모은다."""
        if self._closed or self.value is None or self.error is not None or self.loading:
            return
        self.value = replace(value, revision=self.value.revision)
        self.dirty = True
        self._edited = time.monotonic()

    def tick(self) -> bool:
        """완료된 설정만 호출자에 알리고 저장 실패 시 초안을 유지한다."""
        if self._closed:
            return False
        changed = False
        if self._read_retry.due:
            self._start_read()
        if self._pending is not None and self._pending.done():
            future, self._pending = self._pending, None
            try:
                saved = future.result()
                newer = self._submitted is not None and self.value != self._submitted
                self._read_retry.reset()
                self.confirmed = saved
                self.value = (
                    replace(self.value, revision=saved.revision) if newer and self.value else saved
                )
                self.dirty = newer
                changed = True
            except Exception:
                if self._submitted is None:
                    # 읽기 실패만 재시도한다. 저장 결과가 불명확하면 초안을 그대로 둔다.
                    self.error = "설정을 불러오지 못했습니다. 잠시 후 자동으로 다시 시도합니다."
                    self._read_retry.schedule()
                else:
                    self.error = "저장 여부를 확인하지 못했습니다. 다시 불러와 주세요."
        if (
            self.dirty
            and not self.busy
            and self.error is None
            and time.monotonic() - self._edited >= 0.35
        ):
            self._submitted = self.value
            if self.value is not None:
                self._pending = self._executor.submit(self._client.save, self.value)
        return changed

    @property
    def loading(self) -> bool:
        """초기 조회와 명시적 다시 읽기 동안 입력을 잠근다."""
        return self.retrying or (self._pending is not None and self._submitted is None)

    @property
    def retrying(self) -> bool:
        """설정 조회 재시도를 기다리고 있는지 반환한다."""
        return self._read_retry.waiting

    @property
    def busy(self) -> bool:
        """읽기·저장 진행 여부를 반환한다."""
        return self._pending is not None

    def close(self) -> None:
        """진행 중 요청을 마무리하고 추가 작업을 중단한다."""
        self._closed = True
        self._read_retry.cancel()
        self._executor.shutdown(wait=False, cancel_futures=True)
