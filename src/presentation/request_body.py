"""로컬 HTTP 요청의 본문을 경로별 제한 안에서 읽는다."""

from fastapi import HTTPException, Request


async def read_limited_body(
    request: Request, *, max_bytes: int, too_large: str | dict[str, str]
) -> bytearray:
    """전송 조각을 합산해 한도를 넘는 조각은 본문 버퍼에 복사하기 전에 거부한다."""
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > max_bytes:
            raise HTTPException(413, detail=too_large)
        body.extend(chunk)
    return body
