"""근무 규칙만 보호된 JSON 파일에 버전 조건부로 저장한다."""

import json
from collections.abc import Callable
from contextlib import AbstractContextManager, nullcontext
from dataclasses import replace
from pathlib import Path

from src.application.work_rules_settings import (
    WorkRulesConflictError,
    WorkRulesDocument,
    document_payload,
    parse_document,
)
from src.domain.daily_work import WorkRules
from src.infrastructure.private_files import (
    private_file_lock,
    read_private_file,
    write_private_file,
)

MAX_WORK_RULES_BYTES = 16384


class WorkRulesFile:
    """관리자 API 내부의 동시 쓰기를 직렬화한다."""

    def __init__(
        self,
        path: Path,
        *,
        apply_today: Callable[[WorkRules], AbstractContextManager[None]] | None = None,
    ) -> None:
        """설치 경로를 시작 경계에서 주입한다."""
        self._path = path
        self._apply_today = apply_today
        self._lock_path = path.with_name(f".{path.name}.lock")

    def read(self) -> WorkRulesDocument | None:
        """링크를 따라가지 않고 권한이 제한된 기존 문서만 읽는다."""
        try:
            data = read_private_file(self._path, max_bytes=MAX_WORK_RULES_BYTES)
        except FileNotFoundError:
            return None
        value = parse_document(json.loads(data))
        if value.revision < 1:
            raise ValueError("저장된 근무 규칙의 버전을 확인하세요.")
        return value

    def save(self, document: WorkRulesDocument) -> WorkRulesDocument:
        """현재 버전 확인과 원자 교체를 같은 잠금 안에서 수행한다."""
        # 규칙 파일은 저장 때마다 교체되므로 같은 디렉터리의 고정 잠금 파일을 쓴다.
        with private_file_lock(self._lock_path):
            current = self.read()
            revision = current.revision if current else 0
            if revision != document.revision:
                raise WorkRulesConflictError()
            saved = replace(document, revision=revision + 1)
            published = False
            try:
                # 기록·예약을 먼저 준비하고 파일 교체가 실패하면 DB 변경도 롤백한다.
                with self._apply_today(saved.rules) if self._apply_today else nullcontext():
                    self._write(saved)
                    published = True
            except Exception:
                # 파일 교체 뒤 DB 커밋이 실패하면 다른 작성자가 들어오기 전에 원래 규칙을 복구한다.
                if published:
                    if current is None:
                        self._path.unlink()
                    else:
                        self._write(current)
                raise
            return saved

    def _write(self, document: WorkRulesDocument) -> None:
        """버전과 규칙을 같은 보호 파일에 원자적으로 교체한다."""
        content = json.dumps(document_payload(document), ensure_ascii=False, indent=2) + "\n"
        write_private_file(self._path, content.encode())
