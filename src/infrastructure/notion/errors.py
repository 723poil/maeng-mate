"""Notion 오류를 기술 독립적인 수집 오류 계약으로 전달한다."""

from src.application.errors import SourceReadError


class SourceError(SourceReadError):
    """응답·토큰·식별값 없이 Notion 실패 분류만 보존한다."""
