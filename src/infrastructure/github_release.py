"""공개 배포 저장소에서 사용자가 설치할 정식 릴리스를 조회한다."""

import re

import httpx

REPOSITORY = "723poil/maeng-mate"
LATEST_RELEASE = f"https://github.com/{REPOSITORY}/releases/latest"


def latest_version() -> str:
    """인증 정보 없이 공개한 정식 태그의 숫자 버전만 반환한다."""
    response = httpx.get(
        f"https://api.github.com/repos/{REPOSITORY}/releases/latest",
        headers={"Accept": "application/vnd.github+json"},
        timeout=8,
    )
    response.raise_for_status()
    release: object = response.json()
    tag = release.get("tag_name") if isinstance(release, dict) else None
    if not isinstance(tag, str) or not re.fullmatch(
        r"v(?:0|[1-9][0-9]*)(?:\.(?:0|[1-9][0-9]*)){2}", tag
    ):
        raise ValueError("공개된 버전 번호를 확인할 수 없습니다.")
    return tag[1:]
