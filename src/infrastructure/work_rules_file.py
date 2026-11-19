"""근무 규칙만 보호된 JSON 파일에 버전 조건부로 저장한다."""

import json
from dataclasses import replace
from pathlib import Path

from src.application.work_rules_settings import (
    WorkRulesConflictError,
    WorkRulesDocument,
    document_payload,
    parse_document,
)
from src.infrastructure.private_files import (
    private_file_lock,
    read_private_file,
    write_private_file,
)

MAX_WORK_RULES_BYTES = 16384


class WorkRulesFile:
    """관리자 API 내부의 동시 쓰기를 직렬화한다."""

    def __init__(self, path: Path) -> None:
        """설치 경로를 시작 경계에서 주입한다."""
        self._path = path
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
            write_private_file(
                self._path,
                (json.dumps(document_payload(saved), ensure_ascii=False, indent=2) + "\n").encode(),
            )
            return saved
