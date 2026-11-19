"""命令行入口：``python3 -m cli_manager``（一般通过 ``./start.sh`` 启动）。"""

from __future__ import annotations

import argparse
import os
import sys

from . import APP_NAME, __version__
from .config import ModuleLoader
from .web import ALLOWED_HOSTS, App, serve

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_PORT = 8977


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="cli-manager",
        description=f"{APP_NAME}：从 modules/*.json 加载命令，在本机执行并流式显示输出。",
    )
    parser.add_argument("--host", default="127.0.0.1", help="监听地址，只允许回环地址（默认 127.0.0.1）")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help=f"监听端口（默认 {DEFAULT_PORT}）")
    parser.add_argument(
        "--modules-dir",
        default=os.path.join(ROOT, "modules"),
        help="模块配置目录（默认 <项目>/modules）",
    )
    parser.add_argument("--check", action="store_true", help="只校验模块配置并打印摘要，不启动服务")
    parser.add_argument("--version", action="version", version=f"{APP_NAME} {__version__}")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if args.host not in ALLOWED_HOSTS:
        print(
            f"拒绝启动：只允许监听回环地址 {' / '.join(sorted(ALLOWED_HOSTS))}（当前 --host {args.host}）",
            file=sys.stderr,
        )
        return 2
    if not 1 <= args.port <= 65535:
        print(f"端口不合法：{args.port}", file=sys.stderr)
        return 2

    modules_dir = os.path.abspath(os.path.expanduser(args.modules_dir))
    loader = ModuleLoader(modules_dir, app_root=ROOT)

    if args.check:
        print(loader.describe())
        return 1 if loader.errors else 0

    app = App(loader, web_dir=os.path.join(ROOT, "web"), host=args.host, port=args.port)
    return serve(app, args.host, args.port)


if __name__ == "__main__":
    raise SystemExit(main())
