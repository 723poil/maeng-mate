"""근무 규칙만 보호된 JSON 파일에 버전 조건부로 저장한다."""

import json
import os
import stat
from dataclasses import replace
from pathlib import Path
from threading import Lock

from src.application.work_rules_settings import (
    WorkRulesConflictError,
    WorkRulesDocument,
    document_payload,
    parse_document,
)
from src.infrastructure.private_files import write_private_file

MAX_WORK_RULES_BYTES = 16384


class WorkRulesFile:
    """관리자 API 내부의 동시 쓰기를 직렬화한다."""

    def __init__(self, path: Path) -> None:
        """설치 경로를 시작 경계에서 주입한다."""
        self._path = path
        self._lock = Lock()

    def read(self) -> WorkRulesDocument | None:
        """링크를 따라가지 않고 권한이 제한된 기존 문서만 읽는다."""
        try:
            descriptor = os.open(self._path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        except FileNotFoundError:
            return None
        with os.fdopen(descriptor, "rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
                raise ValueError("근무 규칙 파일의 권한을 확인하세요.")
            data = stream.read(MAX_WORK_RULES_BYTES + 1)
        if len(data) > MAX_WORK_RULES_BYTES:
            raise ValueError("근무 규칙 파일이 너무 큽니다.")
        value = parse_document(json.loads(data))
        if value.revision < 1:
            raise ValueError("저장된 근무 규칙의 버전을 확인하세요.")
        return value

    def save(self, document: WorkRulesDocument) -> WorkRulesDocument:
        """현재 버전 확인과 원자 교체를 같은 잠금 안에서 수행한다."""
        with self._lock:
            current = self.read()
            revision = current.revision if current else 0
            if revision != document.revision:
                raise WorkRulesConflictError()
            saved = replace(document, revision=revision + 1)
            write_private_file(
                self._path,
                (json.dumps(document_payload(saved), ensure_ascii=False, indent=2) + "\n").encode(),
            )
            return saved
