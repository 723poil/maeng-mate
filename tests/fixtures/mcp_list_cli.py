"""임시 프로필과 가상 관리 인증으로 실제 CLI 진입점을 실행한다."""

import os
import sys
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

from Security import kSecAttrAccount, kSecAttrService

from src.application.credentials import CredentialRecord, CredentialSecret
from src.bootstrap.build_profile import DEVELOPMENT, RELEASE, current_profile
from src.bootstrap.environment import mcp_environment
from src.bootstrap.packaged_entry import main as packaged_main
from src.infrastructure.macos_keychain import MacKeychain
from tests.fixtures.mcp_lists import ADMIN


def main() -> None:
    """배포 분기는 모사하되 실제 동봉 바이너리 검증으로 표현하지 않는다."""
    root = Path(os.environ["FIXTURE_PROFILE_ROOT"])
    release = os.environ["FIXTURE_PROFILE"] == "release"
    expected = RELEASE if release else DEVELOPMENT
    reads = 0

    def read(store: MacKeychain, *, allow_prompt: bool = False) -> CredentialRecord:
        """운영 Keychain 대신 올바른 프로필의 가상 키만 반환한다."""
        nonlocal reads
        assert store._lock_path.is_relative_to(root)
        assert store._query[kSecAttrService] == expected.identifier + ".credentials"
        assert store._query[kSecAttrAccount] == "local-api"
        assert not allow_prompt
        reads += 1
        return CredentialRecord(1, CredentialSecret(ADMIN))

    current_profile.cache_clear()
    with ExitStack() as isolated:
        if release:
            # 임시 Info.plist를 읽는 실제 frozen 프로필 선택 분기만 모사한다.
            isolated.enter_context(patch.object(sys, "frozen", True, create=True))
            isolated.enter_context(
                patch.object(
                    sys, "executable", str(root / "Maeng Mate.app/Contents/MacOS/MaengMate")
                )
            )
        isolated.enter_context(
            patch("src.bootstrap.instance.application_directory", return_value=root)
        )
        isolated.enter_context(
            patch("src.bootstrap.app_settings.application_directory", return_value=root)
        )
        isolated.enter_context(
            patch("src.bootstrap.backup_restore.application_directory", return_value=root)
        )
        isolated.enter_context(patch.object(MacKeychain, "read", read))
        assert current_profile() == expected
        assert "LOCAL_API_TOKEN" not in os.environ
        # stdio는 bootstrap.json을 읽지 않으므로 기본 포트와 명시 포트를 별도로 확인한다.
        defaults = mcp_environment({"MCP_ACCESS_TOKEN": ADMIN})
        assert defaults.api_url == f"http://127.0.0.1:{expected.port}"
        args = sys.argv[1:]
        packaged_main()
        assert reads == (0 if args == ["mcp"] else 1)


if __name__ == "__main__":
    main()
