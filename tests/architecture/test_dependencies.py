"""계층 사이의 잘못된 의존이 다시 생기는 것을 방지한다."""

import ast
from pathlib import Path


def test_layer_dependencies_point_inward() -> None:
    """외부 연동·설정 조립이 업무 모델과 유스케이스로 침투하지 못하게 한다."""
    root = Path(__file__).resolve().parents[2] / "src"
    assert root.is_dir(), "src 아래에 역할별 패키지가 필요합니다."
    allowed = {
        "domain": {"domain"},
        "application": {"domain", "application"},
        # 알림 발신 앱과 설정 미리보기는 같은 아이콘 원본을 사용한다.
        "infrastructure": {"domain", "application", "infrastructure", "assets"},
        "presentation": {"domain", "application", "presentation", "assets"},
        "bootstrap": {"domain", "application", "infrastructure", "presentation", "bootstrap"},
    }
    external_io = {
        "fastapi",
        "httpx",
        "sqlite3",
        "pydantic",
        "mcp",
        "uvicorn",
        "os",
        "pathlib",
    }
    failures: list[str] = []
    for layer, dependencies in allowed.items():
        assert (root / layer).is_dir(), f"누락된 계층: {layer}"
        for path in (root / layer).rglob("*.py"):
            for node in ast.walk(ast.parse(path.read_text())):
                imports = []
                if isinstance(node, ast.ImportFrom):
                    assert node.level == 0, "계층 확인을 위해 절대 import를 사용합니다."
                    imports = [node.module or ""]
                elif isinstance(node, ast.Import):
                    imports = [alias.name for alias in node.names]
                for module in imports:
                    parts = module.split(".")
                    if parts[0] == "src":
                        if len(parts) < 2 or parts[1] not in dependencies:
                            failures.append(f"{path.relative_to(root)} -> {module}")
                    elif layer in {"domain", "application"} and parts[0] in external_io:
                        failures.append(f"{path.relative_to(root)} -> {module}")
    assert failures == [], "금지된 의존: " + ", ".join(failures)
