"""사용하는 macOS Security SecItem API의 최소 계약이다."""

from collections.abc import Mapping

kSecClass: str
kSecClassGenericPassword: str
kSecAttrService: str
kSecAttrAccount: str
kSecAttrLabel: str
kSecValueData: str
kSecReturnData: str
kSecMatchLimit: str
kSecMatchLimitOne: str
kSecUseAuthenticationUI: str
kSecUseAuthenticationUIFail: str
errSecItemNotFound: int

def SecItemCopyMatching(query: Mapping[str, object], result: None) -> tuple[int, object]: ...
def SecItemAdd(attributes: Mapping[str, object], result: None) -> tuple[int, object]: ...
def SecItemUpdate(query: Mapping[str, object], attributes: Mapping[str, object]) -> int: ...
