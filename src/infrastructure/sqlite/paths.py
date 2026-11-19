"""SQLite 파일 경로만 허용하는 설정 경계다."""

from pathlib import Path


def validate_database_path(value: str) -> str:
    """URI·외부 주소·메모리 DB를 거부하고 실제 파일 경로로 통일한다."""
    try:
        if (
            not value.strip()
            or "\x00" in value
            or "://" in value
            or value.startswith("file:")
            or value == ":memory:"
        ):
            raise ValueError
        return str(Path(value).expanduser().resolve())
    except (ValueError, OSError, RuntimeError):
        raise ValueError("설정을 확인하세요: DATABASE_PATH") from None
