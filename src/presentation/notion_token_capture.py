"""Notion 토큰 페이지를 연 뒤 새로 복사된 토큰을 클립보드에서 한 번 가져온다."""

import re
import time

from AppKit import NSPasteboard, NSPasteboardTypeString

from src.application.credentials import CredentialError, CredentialSecret, validated_secret

# 개인 토큰은 사용자 권한을 그대로 써 표 공유가 필요 없다. 막힌 워크스페이스는 통합 연결을 쓴다.
PERSONAL_TOKEN_URL = "https://www.notion.so/developers/tokens"
CONNECTION_TOKEN_URL = "https://www.notion.so/developers/connections"
# 사용자가 페이지에서 토큰을 만들고 복사하기에 충분한 시간만 감지한다.
CAPTURE_SECONDS = 600.0
TOKEN_PATTERN = re.compile(r"(?:ntn|secret)_[A-Za-z0-9]{30,}")


class NotionTokenCapture:
    """버튼을 누른 뒤에만 복사 횟수를 보고, 바뀐 경우에만 클립보드 문자열을 읽는다."""

    def __init__(self) -> None:
        """감지 전에는 클립보드를 읽지 않는다."""
        self._board = NSPasteboard.generalPasteboard()
        self._deadline = 0.0
        self._seen = 0
        self._captured: int | None = None
        self.paused = False

    @property
    def active(self) -> bool:
        """시작 뒤 제한 시간이 지나지 않았는지 확인한다."""
        return time.monotonic() < self._deadline

    def start(self) -> None:
        """지금 클립보드에 있던 값은 무시하고 이후 복사만 기다린다."""
        self._deadline = time.monotonic() + CAPTURE_SECONDS
        self._seen = self._board.changeCount()
        self._captured = None
        self.paused = False

    def stop(self) -> None:
        """취소·성공·창 종료 뒤에는 더 읽지 않는다."""
        self._deadline = 0.0
        self.paused = False

    def poll(self) -> CredentialSecret | None:
        """새 복사가 토큰 형식일 때만 값을 돌려주고 확인이 끝날 때까지 멈춘다."""
        if not self.active or self.paused:
            return None
        count = self._board.changeCount()
        if count == self._seen:
            return None
        self._seen = count
        value = self._board.stringForType_(NSPasteboardTypeString)
        if value is None or TOKEN_PATTERN.fullmatch(text := str(value).strip()) is None:
            return None
        try:
            token = validated_secret(text)
        except CredentialError:
            return None
        self._captured = count
        self.paused = True
        return token

    def resume(self) -> None:
        """확인에 실패하면 다음 복사를 다시 기다린다."""
        self._captured = None
        self.paused = False

    def clear_captured(self) -> None:
        """저장한 토큰이 아직 클립보드에 그대로 있을 때만 비워 새 복사를 지우지 않는다."""
        if self._captured is not None and self._board.changeCount() == self._captured:
            self._board.clearContents()
        self._captured = None
