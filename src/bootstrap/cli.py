"""명시적인 명령별로 환경 검증과 실행 수명을 조립한다."""

import argparse
import asyncio
import os
import sys

from src.bootstrap.environment import ConfigError


def main() -> None:
    """오류 원문 없이 종료하고 MCP stdout은 프로토콜 전용으로 유지한다."""
    parser = argparse.ArgumentParser(prog="maeng-mate")
    parser.add_argument(
        "command",
        choices=["api", "migrate", "sync", "mcp", "run", "menubar", "service", "agent", "settings"],
    )
    parser.add_argument("action", nargs="?")
    parser.add_argument("--file", type=str, help="최초로 가져올 업무 설정 JSON")
    args = parser.parse_args()
    if args.command not in {"service", "agent", "settings"} and args.action is not None:
        parser.error("이 명령은 하위 동작을 받지 않습니다.")
    if args.file is not None and (args.command, args.action) != ("settings", "import-work"):
        parser.error("--file은 settings import-work에서만 사용합니다.")
    try:
        if args.command == "settings":
            from src.bootstrap.runtime_settings import import_runtime_settings

            if args.action == "import-work":
                from pathlib import Path

                from src.bootstrap.app_settings import data_environment
                from src.bootstrap.work_settings import import_work_settings

                if not args.file:
                    parser.error("import-work에는 --file <JSON 경로>가 필요합니다.")
                created = import_work_settings(data_environment(os.environ), Path(args.file))
                print(
                    "업무 설정을 DB에 저장했습니다."
                    if created
                    else "기존 DB 업무 설정을 유지합니다."
                )
                return
            if args.action != "import" or args.file is not None:
                parser.error(
                    "settings에는 import 또는 import-work --file <JSON 경로>가 필요합니다."
                )
            from src.bootstrap.app_settings import data_environment

            created = import_runtime_settings(data_environment(os.environ))
            print(
                "일반·Notion 설정을 DB에 저장했습니다." if created else "기존 DB 설정을 유지합니다."
            )
            return
        if args.command == "service":
            from src.bootstrap.service import run_service

            run_service(args.action or "status")
            return
        if args.command == "agent":
            from src.bootstrap.agent import run_agent

            if args.action == "resident":
                run_agent("resident")
            elif args.action == "menubar":
                run_agent("menubar")
            else:
                parser.error("agent에는 resident 또는 menubar가 필요합니다.")
            return
        if args.command == "mcp":
            from src.bootstrap.environment import mcp_environment
            from src.bootstrap.mcp_wiring import run_stdio_mcp

            asyncio.run(run_stdio_mcp(mcp_environment(os.environ)))
        elif args.command == "menubar":
            from src.bootstrap.desktop import run_menubar
            from src.bootstrap.instance import instance_lock

            with instance_lock("menubar"):
                run_menubar(os.environ)
        elif args.command == "run":
            from src.bootstrap.instance import instance_lock
            from src.bootstrap.resident import run_resident

            with instance_lock("resident"):
                asyncio.run(run_resident(os.environ))
        elif args.command == "api":
            import uvicorn

            from src.bootstrap.app_settings import application_environment
            from src.bootstrap.environment import app_environment, required
            from src.bootstrap.notion_credentials import load_notion_environment
            from src.bootstrap.runtime_settings import load_runtime_environment
            from src.bootstrap.wiring import build_api
            from src.bootstrap.work_rules import build_work_rules
            from src.bootstrap.work_settings import load_work_settings
            from src.infrastructure.sqlite.connection import Database

            environment_values = load_notion_environment(
                load_runtime_environment(application_environment(os.environ)),
                keep_api_available=True,
            )
            environment = app_environment(environment_values)
            from src.bootstrap.instance import instance_lock

            with instance_lock("resident"):
                settings = load_work_settings(
                    Database(required(environment_values, "DATABASE_PATH")), prepare_categories=True
                )
                app = build_api(
                    environment,
                    settings,
                    work_rules_service=build_work_rules(settings),
                    notion_legacy_token=os.environ.get("NOTION_TOKEN") or None,
                )
                uvicorn.run(app, host="127.0.0.1", port=environment.port, access_log=False)
        else:
            from src.bootstrap.commands import run_storage

            sys.exit(asyncio.run(run_storage(args.command, os.environ)))
    except ConfigError as error:
        print(str(error), file=sys.stderr)
        sys.exit(1)
    except Exception:
        print("작업에 실패했습니다. 로컬 설정과 연결 상태를 확인하세요.", file=sys.stderr)
        sys.exit(1)
