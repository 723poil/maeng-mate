"""독립 알림 앱의 표시 정보와 stdin 전달을 관리한다."""

import hashlib
import json
import plistlib
import subprocess
import threading
import uuid
from collections.abc import Callable
from pathlib import Path

from src.application.notification_identity import identity_payload, validate_identity
from src.application.notifications import NotificationJob
from src.application.notion_links import notion_link
from src.domain.notification_identity import NotificationIdentity
from src.domain.preferences import Preferences
from src.infrastructure.macos_icon import icon_icns, icon_png
from src.infrastructure.private_files import write_private_file

LSREGISTER = "/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister"


class MacNotificationApp:
    """번들 갱신과 알림 발송이 겹치지 않도록 같은 잠금을 사용한다."""

    def __init__(self, bundle: Path, identifier: str) -> None:
        """고정된 앱 경로를 주입하고 시작 시 OS 권한을 요청하지 않는다."""
        self._bundle = bundle
        self._identifier = identifier
        self._lock = threading.RLock()
        self._active: NotificationIdentity | None = None
        self._revision = -1

    def read_icon(self, path: str) -> bytes:
        """선택 이미지의 원본 위치는 저장하지 않는다."""
        return icon_png(path)

    def _register(self) -> None:
        """로컬 서명을 갱신하고 같은 식별자의 앱 정보만 다시 등록한다."""
        for command in (
            ["/usr/bin/codesign", "--force", "--deep", "--sign", "-", str(self._bundle)],
            [LSREGISTER, "-f", str(self._bundle)],
        ):
            result = subprocess.run(command, capture_output=True, timeout=30, check=False)
            if result.returncode != 0:
                raise ValueError("알림 앱을 등록하지 못했습니다.")

    def apply(self, identity: NotificationIdentity, revision: int = 0) -> None:
        """번들 이름·아이콘만 바꾸고 실패하면 이전 파일을 복원한다."""
        identity = validate_identity(identity)
        with self._lock:
            if revision < self._revision:
                raise ValueError("더 최근의 알림 앱 설정이 적용되었습니다.")
            if self._active == identity:
                self._revision = revision
                return
            plist_path = self._bundle / "Contents/Info.plist"
            icon_path = self._bundle / "Contents/Resources/MaengMate.icns"
            marker = self._bundle.parent / "notification-identity.json"
            if not plist_path.is_file() or self._bundle.is_symlink():
                raise ValueError("알림 앱 설치가 필요합니다. service install을 실행하세요.")
            previous_plist = plist_path.read_bytes()
            previous_icon = icon_path.read_bytes() if icon_path.is_file() else None
            fingerprint = hashlib.sha256(
                json.dumps(identity_payload(identity), sort_keys=True).encode()
            ).hexdigest()
            raw: object = plistlib.loads(previous_plist)
            if not isinstance(raw, dict) or raw.get("CFBundleIdentifier") != self._identifier:
                raise ValueError("알림 앱의 식별자를 확인하세요.")
            current: dict[str, object] = {
                key: value for key, value in raw.items() if isinstance(key, str)
            }
            desired = {
                **current,
                "CFBundleName": identity.name,
                "CFBundleDisplayName": identity.name,
                "CFBundleIconFile": "MaengMate.icns",
            }
            # 설정값이 같아도 업데이트로 기본 도안이 바뀌었으면 등록된 아이콘을 교체한다.
            desired_icon = icon_icns(
                identity.icon_png if identity.icon_png is not None else icon_png()
            )
            if (
                current == desired
                and previous_icon == desired_icon
                and marker.is_file()
                and marker.read_text() == fingerprint
            ):
                self._active = identity
                self._revision = revision
                return
            self._active = None
            try:
                write_private_file(icon_path, desired_icon)
                write_private_file(plist_path, plistlib.dumps(desired))
                self._register()
                write_private_file(marker, fingerprint.encode())
                self._active = identity
                self._revision = revision
            except Exception:
                write_private_file(plist_path, previous_plist)
                if previous_icon is None:
                    icon_path.unlink(missing_ok=True)
                else:
                    write_private_file(icon_path, previous_icon)
                try:
                    self._register()
                except Exception:
                    pass  # 복구를 시도해도 적용 상태는 실패로 유지하며 다른 이름으로 발송하지 않는다.
                raise ValueError("알림 앱 적용을 확인하지 못했습니다.") from None

    def deliver(
        self,
        job: NotificationJob,
        preferences: Callable[[], Preferences],
        authorize: Callable[[NotificationJob], bool],
    ) -> str:
        """긴 대기·등록 뒤 실행권을 재검증하고 같은 잠금 안에서 발신한다."""
        with self._lock:
            saved = preferences()
            self.apply(saved.notification_identity, saved.revision)
            if not authorize(job):
                return "cancelled"
            return self.request(job.id, job.title, job.body, url=job.url)

    def request(
        self,
        identifier: str,
        title: str,
        body: str,
        *,
        authorize_only: bool = False,
        url: str | None = None,
    ) -> str:
        """원문을 명령행·파일에 쓰지 않고 알림 앱 stdin으로만 전달한다."""
        with self._lock:
            if self._active is None:
                return "failed"
            try:
                result = subprocess.run(
                    [str(self._bundle / "Contents/MacOS/NotificationAgent")],
                    input=json.dumps(
                        {
                            "mode": "authorize" if authorize_only else "request",
                            "id": identifier,
                            "title": title,
                            "body": body,
                            "url": notion_link(url),
                        }
                    ),
                    text=True,
                    capture_output=True,
                    timeout=55 if authorize_only else 12,
                    check=False,
                )
                state = result.stdout.strip()
                return (
                    state
                    if result.returncode == 0
                    and state
                    in (
                        {"requested", "failed", "unknown", "denied"}
                        if authorize_only
                        else {"requested", "failed", "unknown"}
                    )
                    else "failed"
                )
            except subprocess.TimeoutExpired:
                return "unknown"
            except OSError:
                return "failed"

    def test(self) -> str:
        """사용자가 누른 테스트에서만 권한 요청을 오래 기다린다."""
        state = self.request("permission", "", "", authorize_only=True)
        if state != "requested":
            return state
        return self.request(
            uuid.uuid4().hex, "테스트 알림", "이 알림의 앱 이름과 아이콘을 확인하세요."
        )
