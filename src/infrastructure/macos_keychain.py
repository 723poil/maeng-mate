"""현재 사용자의 Keychain에서 계정별 자격 정보 문서를 원자 교체한다."""

import fcntl
import json
import os
from pathlib import Path

from Foundation import NSData
from Security import (
    SecItemAdd,
    SecItemCopyMatching,
    SecItemUpdate,
    errSecItemNotFound,
    kSecAttrAccount,
    kSecAttrLabel,
    kSecAttrService,
    kSecClass,
    kSecClassGenericPassword,
    kSecMatchLimit,
    kSecMatchLimitOne,
    kSecReturnData,
    kSecUseAuthenticationUI,
    kSecUseAuthenticationUIFail,
    kSecValueData,
)

from src.application.credentials import (
    CredentialError,
    CredentialRecord,
    CredentialSecret,
    validated_secret,
)


class MacKeychain:
    """읽기 실패를 숨기지 않고 버전 조건부 쓰기를 프로세스 간 직렬화한다."""

    def __init__(
        self,
        lock_path: Path,
        *,
        service: str = "local.maeng-mate.credentials",
        account: str = "notion",
        label: str = "Maeng Mate · Notion",
    ) -> None:
        """앱 전용 항목과 보호된 잠금 경로를 주입한다."""
        self._lock_path = lock_path
        self._label = label
        self._query: dict[str, object] = {
            kSecClass: kSecClassGenericPassword,
            kSecAttrService: service,
            kSecAttrAccount: account,
        }

    def read(self, *, allow_prompt: bool = False) -> CredentialRecord | None:
        """자동 조회에서 인증 창을 반복 표시하지 않고 접근 실패를 알린다."""
        status, result = SecItemCopyMatching(
            {
                **self._query,
                kSecReturnData: True,
                kSecMatchLimit: kSecMatchLimitOne,
                **({} if allow_prompt else {kSecUseAuthenticationUI: kSecUseAuthenticationUIFail}),
            },
            None,
        )
        if status == errSecItemNotFound:
            return None
        if status != 0 or not isinstance(result, NSData):
            raise CredentialError("keychain")
        try:
            data = bytes(result)
            if len(data) > 16384:
                raise ValueError
            raw: object = json.loads(data)
            if (
                not isinstance(raw, dict)
                or set(raw) != {"version", "revision", "token"}
                or type(raw["version"]) is not int
                or raw["version"] != 1
            ):
                raise ValueError
            revision = raw["revision"]
            if type(revision) is not int or revision < 1:
                raise ValueError
            return CredentialRecord(
                revision, None if raw["token"] is None else validated_secret(raw["token"])
            )
        except (ValueError, UnicodeError, CredentialError):
            raise CredentialError("keychain") from None

    def replace(self, expected_revision: int, token: CredentialSecret | None) -> CredentialRecord:
        """전체 문서를 한 번 교체하고 삭제 표시도 같은 버전 조건으로 저장한다."""
        if token is not None:
            token = validated_secret(token.value)
        descriptor: int | None = None
        try:
            self._lock_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            descriptor = os.open(self._lock_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
            info = os.fstat(descriptor)
            if info.st_uid != os.getuid() or info.st_mode & 0o077:
                raise CredentialError("keychain")
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise CredentialError("busy") from None
            current = self.read()
            if (current.revision if current else 0) != expected_revision:
                raise CredentialError("conflict")
            saved = CredentialRecord(expected_revision + 1, token)
            data = json.dumps(
                {"version": 1, "revision": saved.revision, "token": token.value if token else None}
            ).encode()
            if current is None:
                status, _ = SecItemAdd(
                    {**self._query, kSecAttrLabel: self._label, kSecValueData: data}, None
                )
            else:
                status = SecItemUpdate(self._query, {kSecValueData: data})
            if status != 0:
                raise CredentialError("keychain")
            return saved
        except OSError:
            raise CredentialError("keychain") from None
        finally:
            if descriptor is not None:
                os.close(descriptor)
