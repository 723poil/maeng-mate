"""실제 자격 정보 유스케이스와 HTTP 경계의 공통 오류 계약을 확인한다."""

import secrets
from pathlib import Path

import httpx
import pytest

from src.application.credentials import CredentialSecret
from tests.fixtures.credential_errors import (
    FileCredentials,
    credential_files,
    database_rows,
    prepare,
)


@pytest.mark.parametrize(
    "code,status",
    [
        ("invalid", 422),
        ("missing", 422),
        ("unauthorized", 422),
        ("permission", 422),
        ("conflict", 409),
        ("keychain", 503),
        ("busy", 503),
        ("unavailable", 503),
        ("timeout", 503),
        ("rate_limited", 503),
    ],
)
async def test_check_errors_match_and_preserve_storage(
    tmp_path: Path, code: str, status: int
) -> None:
    """두 확인 경로가 같은 공개 코드를 반환하고 저장 자료를 보존한다."""
    admin = secrets.token_urlsafe(48)
    app = await prepare(tmp_path, admin)
    revision = 0
    if code not in {"missing", "conflict", "invalid"}:
        for provider in ("notion", "slack"):
            FileCredentials(tmp_path / f"{provider}.json").replace(
                0, CredentialSecret(secrets.token_urlsafe(48))
            )
        revision = 1
        (tmp_path / "failure").write_text(code)
    before = credential_files(tmp_path)
    stored = database_rows(tmp_path)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://fixture",
        headers={"Authorization": f"Bearer {admin}"},
    ) as client:
        for path in ("/v1/notion/credentials/check", "/v1/slack/settings/check"):
            response = await client.post(
                path,
                json={
                    "revision": -1 if code == "invalid" else 99 if code == "conflict" else revision
                },
            )
            assert response.status_code == status, (path, response.status_code, response.json())
            assert response.json() == {"detail": {"code": code}}
            assert response.headers["cache-control"] == "no-store"
    assert before == credential_files(tmp_path)
    assert stored == database_rows(tmp_path)


async def test_success_delete_conflict_and_slack_specific_errors(tmp_path: Path) -> None:
    """저장·확인·삭제와 Slack 발송 전용 오류의 기존 계약을 유지한다."""
    admin = secrets.token_urlsafe(48)
    app = await prepare(tmp_path, admin)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://fixture",
        headers={"Authorization": f"Bearer {admin}"},
    ) as client:
        for base, save_path, save_method, delete_path, delete_method, revision_key in (
            (
                "/v1/notion/credentials",
                "/v1/notion/credentials",
                "PUT",
                "/v1/notion/credentials",
                "DELETE",
                "revision",
            ),
            (
                "/v1/slack/settings",
                "/v1/slack/settings/token",
                "POST",
                "/v1/slack/settings/delete",
                "POST",
                "credentialRevision",
            ),
        ):
            saved = await client.request(
                save_method, save_path, json={"revision": 0, "token": secrets.token_urlsafe(48)}
            )
            assert saved.status_code == 200
            assert saved.json()[revision_key] == 1
            checked = await client.post(base + "/check", json={"revision": 1})
            assert checked.status_code == 200 and checked.json()["verification"] == "ok"
            conflict = await client.post(base + "/check", json={"revision": 0})
            assert conflict.status_code == 409 and conflict.json() == {
                "detail": {"code": "conflict"}
            }
            deleted = await client.request(delete_method, delete_path, json={"revision": 1})
            assert deleted.status_code == 200 and not deleted.json()["configured"]
            missing = await client.post(base + "/check", json={"revision": 2})
            assert missing.status_code == 422 and missing.json() == {"detail": {"code": "missing"}}

        recipient = await client.post(
            "/v1/slack/settings/test", json={"revision": 0, "credentialRevision": 2}
        )
        assert recipient.status_code == 503 and recipient.json() == {
            "detail": {"code": "recipient"}
        }
        assert (
            await client.post(
                "/v1/slack/settings/member", json={"revision": 0, "userId": "U12345678"}
            )
        ).status_code == 200
        unverified = await client.post(
            "/v1/slack/settings/test", json={"revision": 1, "credentialRevision": 2}
        )
        assert unverified.status_code == 503 and unverified.json() == {
            "detail": {"code": "unverified"}
        }
        assert (
            await client.post(
                "/v1/slack/settings/token", json={"revision": 2, "token": secrets.token_urlsafe(48)}
            )
        ).status_code == 200
        unknown = await client.post(
            "/v1/slack/settings/test", json={"revision": 1, "credentialRevision": 3}
        )
        assert unknown.status_code == 503 and unknown.json() == {
            "detail": {"code": "delivery_unknown"}
        }


async def test_check_validation_and_authentication(tmp_path: Path) -> None:
    """관리 인증·본문 제한 실패는 자격 정보 검사와 분리한다."""
    admin = secrets.token_urlsafe(48)
    app = await prepare(tmp_path, admin)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://fixture"
    ) as client:
        for path in ("/v1/notion/credentials/check", "/v1/slack/settings/check"):
            assert (await client.post(path, json={"revision": 0})).status_code == 401
            client.headers["Authorization"] = f"Bearer {admin}"
            for body in (b"{", b'{"revision":true}', b'{"revision":0,"extra":1}'):
                response = await client.post(path, content=body)
                assert response.status_code == 422 and response.json() == {
                    "detail": {"code": "invalid"}
                }
            large = await client.post(path, content=b"x" * 4097)
            assert large.status_code == 413 and large.json() == {"detail": {"code": "invalid"}}
            del client.headers["Authorization"]


@pytest.mark.parametrize("code,status", [("conflict", 409), ("keychain", 503), ("busy", 503)])
async def test_read_and_authorize_share_credential_policy(
    tmp_path: Path, code: str, status: int
) -> None:
    """조회와 명시적 승인도 저장소 오류에 같은 HTTP 정책을 적용한다."""
    admin = secrets.token_urlsafe(48)
    app = await prepare(tmp_path, admin)
    (tmp_path / "store-failure").write_text(code)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://fixture",
        headers={"Authorization": f"Bearer {admin}"},
    ) as client:
        for base in ("/v1/notion/credentials", "/v1/slack/settings"):
            for method, path, body in (("GET", base, None), ("POST", base + "/authorize", {})):
                response = await client.request(method, path, json=body)
                assert response.status_code == status and response.json() == {
                    "detail": {"code": code}
                }
