"""시작 보정 전에 로컬 API 포트를 확보하고 실행 수명 동안 유지한다."""

import socket
from collections.abc import Iterator
from contextlib import contextmanager

from src.bootstrap.environment import ConfigError


@contextmanager
def local_listener(port: int) -> Iterator[socket.socket]:
    """바인딩에 성공한 리스너만 전달하고 실패·정상 종료 모두 소켓을 닫는다."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        try:
            # 직전 연결의 TIME_WAIT 때문에 정상 재시작이 막히지 않게 한다.
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            listener.bind(("127.0.0.1", port))
            listener.listen(socket.SOMAXCONN)
            listener.setblocking(False)
        except OSError:
            raise ConfigError("로컬 API 포트를 사용할 수 없습니다: LOCAL_API_PORT") from None
        # 호출자 보정 중의 오류를 포트 오류로 바꾸지 않고 원래 정리 경로로 전달한다.
        yield listener
