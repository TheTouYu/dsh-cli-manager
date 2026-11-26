"""HTTP 服务：静态页面 + JSON API + SSE。

只监听回环地址；所有写操作要求自定义头 ``X-Requested-With: cli-manager``，
并校验 Host / Origin / Content-Type —— 浏览器里的跨站页面无法伪造这些组合，
以此防 CSRF / DNS rebinding。
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

from . import APP_NAME, __version__
from .config import (
    RISK_LEVELS,
    ConfigError,
    Module,
    ModuleLoader,
    current_platform,
    platform_available,
)
from .runner import Run, RunRegistry

ALLOWED_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
MAX_BODY_BYTES = 65_536
CSRF_HEADER = "cli-manager"

RUN_EVENTS_RE = re.compile(r"^/api/runs/([0-9a-f]{16})/events$")
RUN_CANCEL_RE = re.compile(r"^/api/runs/([0-9a-f]{16})/cancel$")
RUN_RE = re.compile(r"^/api/runs/([0-9a-f]{16})$")

CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".ico": "image/x-icon",
    ".woff2": "font/woff2",
}

CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
    "font-src 'self'; connect-src 'self'; base-uri 'none'; form-action 'none'; "
    "frame-ancestors 'none'"
)


class ApiError(Exception):
    def __init__(
        self,
        status: int,
        message: str,
        code: str | None = None,
        details: dict[str, Any] | None = None,
    ):
        super().__init__(message)
        self.status = status
        self.message = message
        self.code = code
        self.details = details or {}


class App:
    def __init__(self, loader: ModuleLoader, web_dir: str, host: str = "127.0.0.1", port: int = 8977):
        self.loader = loader
        self.web_dir = os.path.abspath(web_dir)
        self.runs = RunRegistry()
        self.host = host
        self.port = port
        self.started = time.monotonic()

    def log(self, message: str) -> None:
        print(f"[{datetime.now():%H:%M:%S}] {message}", file=sys.stderr, flush=True)


def make_handler(app: App) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        server_version = f"CliManager/{__version__}"
        protocol_version = "HTTP/1.1"

        # ---------------- 基础设施 ----------------
        def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A003
            sys.stderr.write(f"[{datetime.now():%H:%M:%S}] {fmt % args}\n")

        def finish(self) -> None:
            try:
                super().finish()
            except (BrokenPipeError, ConnectionResetError):
                pass

        def _headers_common(self) -> None:
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")

        def _send_bytes(
            self,
            status: int,
            body: bytes,
            content_type: str,
            extra: dict[str, str] | None = None,
        ) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self._headers_common()
            for key, value in (extra or {}).items():
                self.send_header(key, value)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def _send_json(self, status: int, payload: dict[str, Any]) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self._send_bytes(status, body, "application/json; charset=utf-8")

        def _send_error_json(
            self,
            status: int,
            message: str,
            code: str | None = None,
            details: dict[str, Any] | None = None,
        ) -> None:
            payload: dict[str, Any] = {"error": message}
            if code:
                payload["code"] = code
            if details:
                payload["details"] = details
            # 出错时可能还有未读的请求体，关闭连接避免 keep-alive 串包
            self.close_connection = True
            self._send_json(status, payload)

        def _host_ok(self) -> bool:
            host = (self.headers.get("Host") or "").strip()
            port = self.app.port
            allowed = {
                f"127.0.0.1:{port}",
                f"localhost:{port}",
                f"[::1]:{port}",
                "127.0.0.1",
                "localhost",
            }
            return host in allowed

        def _post_guard(self) -> None:
            if (self.headers.get("X-Requested-With") or "").strip() != CSRF_HEADER:
                raise ApiError(403, f"缺少 X-Requested-With: {CSRF_HEADER} 请求头（防 CSRF）", "csrf")
            origin = (self.headers.get("Origin") or "").strip()
            if origin:
                port = self.app.port
                allowed = {
                    f"http://127.0.0.1:{port}",
                    f"http://localhost:{port}",
                }
                if origin not in allowed:
                    raise ApiError(403, f"不允许的 Origin：{origin}", "bad_origin")
            ctype = (self.headers.get("Content-Type") or "").split(";", 1)[0].strip().lower()
            if ctype != "application/json":
                raise ApiError(403, "Content-Type 必须是 application/json", "bad_content_type")

        def _read_json(self) -> dict[str, Any]:
            raw_length = self.headers.get("Content-Length")
            if raw_length is None:
                raise ApiError(411, "缺少 Content-Length", "length_required")
            try:
                length = int(raw_length)
            except ValueError as exc:
                raise ApiError(400, "Content-Length 不合法", "bad_length") from exc
            if length < 0 or length > MAX_BODY_BYTES:
                raise ApiError(413, f"请求体超出 {MAX_BODY_BYTES} 字节上限", "too_large")
            raw = self.rfile.read(length)
            if not raw:
                return {}
            try:
                parsed = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ApiError(400, f"请求体不是合法 JSON：{exc}", "bad_json") from exc
            if not isinstance(parsed, dict):
                raise ApiError(400, "请求体必须是 JSON 对象", "bad_json")
            return parsed

        # ---------------- 路由 ----------------
        def do_GET(self) -> None:  # noqa: N802
            try:
                if not self._host_ok():
                    raise ApiError(403, f"不允许的 Host 头：{self.headers.get('Host')!r}（只接受本机回环）", "bad_host")
                self._handle_get()
            except ApiError as exc:
                self._send_error_json(exc.status, exc.message, exc.code, exc.details)
            except (BrokenPipeError, ConnectionResetError):
                pass
            except Exception as exc:  # pragma: no cover - 兜底
                self.app.log(f"GET {self.path} 内部错误：{exc!r}")
                self._send_error_json(500, f"服务器内部错误：{exc}", "internal_error")

        def do_HEAD(self) -> None:  # noqa: N802
            # 只用于探活/健康检查；复用 GET 路由，_send_bytes 会自动省略响应体
            self.do_GET()

        def do_POST(self) -> None:  # noqa: N802
            try:
                if not self._host_ok():
                    raise ApiError(403, f"不允许的 Host 头：{self.headers.get('Host')!r}（只接受本机回环）", "bad_host")
                self._post_guard()
                self._handle_post()
            except ApiError as exc:
                self._send_error_json(exc.status, exc.message, exc.code, exc.details)
            except (BrokenPipeError, ConnectionResetError):
                pass
            except Exception as exc:  # pragma: no cover - 兜底
                self.app.log(f"POST {self.path} 内部错误：{exc!r}")
                self._send_error_json(500, f"服务器内部错误：{exc}", "internal_error")

        def _handle_get(self) -> None:
            parsed = urlparse(self.path)
            path = parsed.path
            query = parse_qs(parsed.query)

            if path in ("/", "/index.html"):
                return self._serve_static("index.html")
            if path.startswith("/static/"):
                return self._serve_static(path[len("/static/") :])
            if path == "/api/health":
                return self._api_health()
            if path == "/api/modules":
                return self._api_modules()
            match = RUN_EVENTS_RE.match(path)
            if match:
                return self._serve_events(match.group(1), query)
            match = RUN_RE.match(path)
            if match:
                return self._api_run_snapshot(match.group(1))
            raise ApiError(404, f"未知路径：{path}", "not_found")

        def _handle_post(self) -> None:
            parsed = urlparse(self.path)
            path = parsed.path
            if path == "/api/render":
                return self._api_render()
            if path == "/api/run":
                return self._api_run()
            if path == "/api/reload":
                self.app.loader.load(force=True)
                self.app.log("手动重载模块配置")
                return self._api_modules()
            match = RUN_CANCEL_RE.match(path)
            if match:
                return self._api_cancel(match.group(1))
            raise ApiError(404, f"未知路径：{path}", "not_found")

        # ---------------- API ----------------
        def _api_health(self) -> None:
            self._send_json(
                200,
                {
                    "ok": True,
                    "app": "dsh-cli-manager",
                    "name": APP_NAME,
                    "version": __version__,
                    "pid": os.getpid(),
                    "port": self.app.port,
                    "uptimeSec": round(time.monotonic() - self.app.started, 1),
                    "moduleCount": len(self.app.loader.snapshot()["modules"]),
                    "runCount": self.app.runs.count(),
                },
            )

        def _api_modules(self) -> None:
            payload = self.app.loader.snapshot()
            payload.update(
                {
                    "app": "dsh-cli-manager",
                    "name": APP_NAME,
                    "version": __version__,
                    "pid": os.getpid(),
                    "port": self.app.port,
                }
            )
            self._send_json(200, payload)

        def _resolve_command(self, body: dict[str, Any]) -> tuple[Module, Any]:
            module_id = body.get("moduleId")
            command_id = body.get("commandId")
            if not isinstance(module_id, str) or not isinstance(command_id, str):
                raise ApiError(400, "缺少 moduleId / commandId", "bad_request")
            self.app.loader.ensure_fresh()
            module = self.app.loader.find_module(module_id)
            if module is None:
                raise ApiError(404, f"模块不存在：{module_id}", "module_not_found")
            command = module.find_command(command_id)
            if command is None:
                raise ApiError(404, f"命令不存在：{module_id}/{command_id}", "command_not_found")
            return module, command

        @staticmethod
        def _params_of(body: dict[str, Any]) -> dict[str, Any]:
            params = body.get("params", {})
            if params is None:
                return {}
            if not isinstance(params, dict):
                raise ApiError(400, "params 必须是 JSON 对象", "invalid_params")
            return params

        def _render_payload(self, module: Module, command: Any) -> dict[str, Any]:
            return {
                "moduleId": module.id,
                "commandId": command.id,
                "risk": command.risk,
                "riskLabel": RISK_LEVELS[command.risk],
                "needsConfirm": command.needs_confirm,
                "confirmHint": command.confirm_hint,
                "timeoutSec": command.timeout_sec,
                "cwd": module.cwd,
                "cwdExists": module.cwd_exists,
            }

        def _api_render(self) -> None:
            body = self._read_json()
            module, command = self._resolve_command(body)
            try:
                rendered = command.render(self._params_of(body))
            except ConfigError as exc:
                raise ApiError(400, str(exc), "invalid_params") from exc
            payload = self._render_payload(module, command)
            payload.update(
                {
                    "display": rendered.display,
                    "argv": list(rendered.argv),
                    "usesShell": rendered.uses_shell,
                    "params": rendered.params,
                }
            )
            self._send_json(200, payload)

        def _api_run(self) -> None:
            body = self._read_json()
            module, command = self._resolve_command(body)
            if not platform_available(command.platform):
                raise ApiError(
                    400,
                    f"该命令仅适用于 {command.platform}（当前 {current_platform()}）",
                    "platform_mismatch",
                    {"platform": command.platform, "current": current_platform()},
                )
            try:
                rendered = command.render(self._params_of(body))
            except ConfigError as exc:
                raise ApiError(400, str(exc), "invalid_params") from exc

            if not module.cwd_exists:
                raise ApiError(
                    400,
                    f"模块工作目录不存在：{module.cwd}",
                    "cwd_missing",
                    {"cwd": module.cwd},
                )
            if command.needs_confirm and body.get("confirm") is not True:
                raise ApiError(
                    400,
                    "该命令危险级为「中/高」，请求必须带 confirm: true（二次确认）",
                    "confirm_required",
                    {
                        "display": rendered.display,
                        "risk": command.risk,
                        "riskLabel": RISK_LEVELS[command.risk],
                        "confirmHint": command.confirm_hint,
                    },
                )

            run = self.app.runs.create(module, command, rendered)
            self.app.log(
                f"执行 {module.id}/{command.id} run={run.run_id} "
                f"risk={command.risk} cwd={module.cwd}"
            )
            self._send_json(
                200,
                {
                    "runId": run.run_id,
                    "display": rendered.display,
                    "eventsUrl": f"/api/runs/{run.run_id}/events?from=0",
                    "refreshUrl": f"/api/runs/{run.run_id}",
                    "run": run.snapshot(),
                },
            )

        def _api_run_snapshot(self, run_id: str) -> None:
            run = self.app.runs.get(run_id)
            if run is None:
                raise ApiError(404, f"运行记录不存在：{run_id}", "run_not_found")
            events, truncated = run.events_snapshot()
            self._send_json(
                200,
                {"run": run.snapshot(), "events": events, "eventsTruncated": truncated},
            )

        def _api_cancel(self, run_id: str) -> None:
            run = self.app.runs.get(run_id)
            if run is None:
                raise ApiError(404, f"运行记录不存在：{run_id}", "run_not_found")
            ok = run.request_cancel()
            self.app.log(f"请求取消 run={run_id} ok={ok}")
            self._send_json(200, {"ok": ok, "cancelled": ok, "run": run.snapshot()})

        # ---------------- SSE ----------------
        def _serve_events(self, run_id: str, query: dict[str, list[str]]) -> None:
            run = self.app.runs.get(run_id)
            if run is None:
                raise ApiError(404, f"运行记录不存在：{run_id}", "run_not_found")

            from_index = 0
            last_id = (self.headers.get("Last-Event-ID") or "").strip()
            if last_id.isdigit():
                from_index = int(last_id) + 1
            elif "from" in query:
                try:
                    from_index = max(0, int(query["from"][0]))
                except (ValueError, IndexError):
                    from_index = 0

            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache, no-transform")
            self.send_header("X-Accel-Buffering", "no")
            self.send_header("Connection", "close")
            self._headers_common()
            self.end_headers()
            self.close_connection = True

            try:
                while True:
                    events, finished = run.wait_events(from_index, timeout=10.0)
                    if events:
                        chunks = []
                        for event in events:
                            data = json.dumps(event.data, ensure_ascii=False)
                            chunks.append(
                                f"id: {event.seq}\nevent: {event.type}\ndata: {data}\n\n"
                            )
                        self.wfile.write("".join(chunks).encode("utf-8"))
                        self.wfile.flush()
                        from_index += len(events)
                    if finished and not run.wait_events(from_index, timeout=0.0)[0]:
                        self.wfile.write(b'event: end\ndata: {"finished": true}\n\n')
                        self.wfile.flush()
                        return
                    if not events:
                        self.wfile.write(b": keep-alive\n\n")
                        self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                return

        # ---------------- 静态文件 ----------------
        def _serve_static(self, relative: str) -> None:
            relative = relative.split("?", 1)[0]
            target = os.path.realpath(os.path.join(self.app.web_dir, relative or "index.html"))
            web_root = self.app.web_dir
            if target != web_root and not target.startswith(web_root + os.sep):
                raise ApiError(403, "非法路径", "forbidden")
            if not os.path.isfile(target):
                raise ApiError(404, f"静态文件不存在：{relative}", "not_found")
            ext = os.path.splitext(target)[1].lower()
            content_type = CONTENT_TYPES.get(ext, "application/octet-stream")
            with open(target, "rb") as fh:
                body = fh.read()
            extra: dict[str, str] = {}
            if ext == ".html":
                extra = {
                    "Content-Security-Policy": CSP,
                    "X-Frame-Options": "DENY",
                }
            self._send_bytes(200, body, content_type, extra)

    Handler.app = app  # type: ignore[attr-defined]
    return Handler


def serve(app: App, host: str, port: int) -> int:
    try:
        httpd = ThreadingHTTPServer((host, port), make_handler(app))
    except OSError as exc:
        print(f"启动失败：无法监听 {host}:{port}（{exc}）", file=sys.stderr)
        print("提示：端口被占用时可用 --port 8978 换一个。", file=sys.stderr)
        return 2
    httpd.daemon_threads = True
    app.port = httpd.server_address[1]

    snapshot = app.loader.snapshot()
    print(f"{APP_NAME} {__version__}")
    print(f"  地址: http://{host}:{app.port}")
    print(f"  模块目录: {app.loader.modules_dir}")
    for module in snapshot["modules"]:
        missing = "" if module["cwdExists"] else "  [cwd 不存在]"
        print(f"  · {module['name']}（{module['id']}）：{len(module['commands'])} 条命令{missing}")
    if snapshot["errors"]:
        print(f"  ! {len(snapshot['errors'])} 个模块加载失败，详见页面顶部提示", file=sys.stderr)
    print("  按 Ctrl+C 停止")

    try:
        httpd.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        print("\n收到 Ctrl+C，正在退出…")
    finally:
        httpd.server_close()
    return 0
