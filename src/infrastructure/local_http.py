"""앱 내부 루프백 HTTP 요청의 인증과 응답 읽기를 담당한다."""

import httpx


def local_client(
    address: str, token: str, *, timeout: float, transport: httpx.BaseTransport | None = None
) -> httpx.Client:
    """조립부가 검증한 주소에만 연결하고 요청별 제한 시간을 적용한다."""
    return httpx.Client(
        base_url=address,
        headers={"Authorization": f"Bearer {token}"},
        trust_env=False,
        follow_redirects=False,
        timeout=timeout,
        transport=transport,
    )


def response_json(response: httpx.Response, *, max_bytes: int) -> object:
    """경로별 허용 크기를 확인한 뒤 호출부에서 검사할 JSON 값을 반환한다."""
    if len(response.content) > max_bytes:
        raise ValueError
    value: object = response.json()
    return value
