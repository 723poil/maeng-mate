"""소유자가 목록 전용 접근을 명시적으로 발급·확인·폐기하는 CLI다."""

import json
from collections.abc import Mapping
from pathlib import Path

import httpx

from src.bootstrap.environment import ConfigError, local_api_settings
from src.infrastructure.local_http import local_client, response_json


def run_list_access(env: Mapping[str, str], file: str | None) -> None:
    """시작 경계가 주입한 관리 인증으로만 로컬 API를 사용한다."""
    port, token = local_api_settings(env)
    try:
        request: object = {"action": "catalog"}
        if file is not None:
            path = Path(file)
            if path.stat().st_size > 16384:
                raise ValueError
            request = json.loads(path.read_bytes())
        if not isinstance(request, dict):
            raise ValueError
        action = request.get("action")
        if not isinstance(action, str):
            raise ValueError
        payload: dict[str, object] | None = None
        method, route = "GET", "/v1/mcp/list-access/"
        if action in {"catalog", "tokens"} and set(request) == {"action"}:
            route += str(action)
        elif action == "issue":
            method, route = "POST", route + "tokens"
            payload = {
                key: value
                for key, value in request.items()
                if isinstance(key, str) and key != "action"
            }
        elif action == "revoke" and set(request) == {"action", "id"}:
            identifier = request["id"]
            if (
                not isinstance(identifier, str)
                or len(identifier) != 32
                or any(c not in "0123456789abcdef" for c in identifier)
            ):
                raise ValueError
            method, route = "POST", route + "tokens/" + identifier + "/revoke"
        else:
            raise ValueError
        with local_client(f"http://127.0.0.1:{port}", token, timeout=10) as client:
            response = client.request(method, route, json=payload)
            response.raise_for_status()
            value = response_json(response, max_bytes=1024 * 1024)
        # 원문은 발급 때 한 번 출력한다. 파일·명령행·별도 로그에는 쓰지 않는다.
        print(json.dumps(value, ensure_ascii=False))
    except (OSError, ValueError, httpx.HTTPError):
        raise ConfigError(
            "목록 접근 요청·앱 연결을 확인하세요. 발급 실패는 목록 확인 후 다시 시도하세요."
        ) from None
