"""격리 개발 앱에서만 사용하는 파일 자격 정보와 외부 인증 원천이다."""

import asyncio
import json
import os
import sqlite3
from pathlib import Path
from threading import Lock

import uvicorn
from fastapi import FastAPI

from src.application.credentials import CredentialError, CredentialRecord, CredentialSecret
from src.application.notion_credentials import NotionCredentialsService
from src.application.runtime_settings_service import RuntimeSettingsService
from src.application.slack_settings import SlackIdentity, SlackSettingsService, SlackTestError
from src.domain.runtime_settings import RuntimeSettings
from src.infrastructure.sqlite.connection import Database
from src.infrastructure.sqlite.preferences import SQLitePreferences
from src.infrastructure.sqlite.runtime_settings import SQLiteRuntimeSettings
from src.infrastructure.sqlite.schema import migrate
from src.presentation.http import create_app


def database_rows(root: Path) -> tuple[str, ...]:
    """검사 전후의 일반 설정·스냅샷·알림 행을 읽기 전용으로 비교한다."""
    with sqlite3.connect((root / "data.sqlite3").as_uri() + "?mode=ro", uri=True) as connection:
        return tuple(connection.iterdump())


def credential_files(root: Path) -> dict[str, bytes]:
    """격리 자격 정보 파일의 원문은 출력하지 않고 보존 여부만 비교한다."""
    return {path.name: path.read_bytes() for path in root.glob("*.json")}


class FileCredentials:
    """사용자 Keychain을 건드리지 않고 임시 파일에만 버전과 가상 토큰을 저장한다."""

    def __init__(self, path: Path) -> None:
        """호출자가 준비한 격리 폴더만 사용한다."""
        self.path = path
        self._lock = Lock()

    def read(self, *, allow_prompt: bool = False) -> CredentialRecord | None:
        """미등록과 명시적 삭제 표시를 구별한다."""
        failure = self.path.parent / "store-failure"
        if failure.exists():
            raise CredentialError(failure.read_text().strip())
        if not self.path.exists():
            return None
        raw: object = json.loads(self.path.read_text())
        if not isinstance(raw, dict):
            raise ValueError("fixture credential object")
        revision, token = raw.get("revision"), raw.get("token")
        if type(revision) is not int or (token is not None and not isinstance(token, str)):
            raise ValueError("fixture credential fields")
        return CredentialRecord(revision, CredentialSecret(token) if token is not None else None)

    def replace(self, expected_revision: int, token: CredentialSecret | None) -> CredentialRecord:
        """외부 확인이 끝난 뒤에만 파일을 조건부 교체한다."""
        with self._lock:
            current = self.read()
            if (current.revision if current else 0) != expected_revision:
                raise CredentialError("conflict")
            saved = CredentialRecord(expected_revision + 1, token)
            self.path.touch(mode=0o600, exist_ok=True)
            self.path.write_text(
                json.dumps({"revision": saved.revision, "token": token.value if token else None})
            )
            return saved


def verify_failure(root: Path) -> None:
    """서버 프로세스를 다시 띄우지 않고 외부 실패만 선택한다."""
    failure = root / "failure"
    if failure.exists():
        raise CredentialError(failure.read_text().strip())


class NotionChecker:
    """실제 Notion 호출 없이 공개 오류만 재현한다."""

    def __init__(self, root: Path) -> None:
        """시나리오 파일이 있는 임시 경로만 받는다."""
        self.root = root

    def verify(self, token: CredentialSecret) -> None:
        """외부 API 원문이나 토큰을 출력하지 않는다."""
        verify_failure(self.root)


class SlackChecker:
    """DM 없이 봇 인증의 성공과 실패를 재현한다."""

    def __init__(self, root: Path) -> None:
        """실패 제어 파일만 공유한다."""
        self.root = root

    def verify(self, token: CredentialSecret) -> SlackIdentity:
        """가상 봇 표시 정보만 반환한다."""
        verify_failure(self.root)
        return SlackIdentity("가상 공간", "가상 봇")


class TestSender:
    """실제 발송을 금지하고 결과 미확인 오류만 제공한다."""

    def send_test(self, token: CredentialSecret, user_id: str) -> None:
        """가상 수신자에 대해서도 외부 통신은 만들지 않는다."""
        raise SlackTestError("delivery_unknown")


async def prepare(root: Path, admin: str) -> FastAPI:
    """실제 유스케이스·SQLite·HTTP 경계에 가상 외부 포트를 연결한다."""
    await asyncio.to_thread(root.mkdir, mode=0o700, parents=True, exist_ok=True)
    database = Database(str(root / "data.sqlite3"))
    await migrate(database)
    runtime = SQLiteRuntimeSettings(database)
    active = RuntimeSettings()
    notion = NotionCredentialsService(
        FileCredentials(root / "notion.json"), NotionChecker(root), legacy=None, active=None
    )
    slack = SlackSettingsService(
        runtime,
        active,
        FileCredentials(root / "slack.json"),
        SlackChecker(root),
        FileCredentials(root / "slack-app.json"),
        TestSender(),
    )
    return create_app(
        admin,
        lambda: {"phase": "fixture"},
        notion_credentials_service=notion,
        slack_settings_service=slack,
        preferences_store=SQLitePreferences(database),
        runtime_settings_service=RuntimeSettingsService(runtime, active),
    )


async def run() -> None:
    """명시한 임시 폴더와 고유 루프백 포트에서만 개발 API를 실행한다."""
    app = await prepare(Path(os.environ["FIXTURE_ROOT"]), os.environ["LOCAL_API_TOKEN"])
    await uvicorn.Server(
        uvicorn.Config(
            app,
            host="127.0.0.1",
            port=int(os.environ["FIXTURE_PORT"]),
            access_log=False,
            log_level="warning",
        )
    ).serve()


if __name__ == "__main__":
    asyncio.run(run())
