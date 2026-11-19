#!/usr/bin/env python3
"""dsh-cli-manager 机检脚本：对一个已经运行的本机实例做 API / SSE / 页面断言。

用法::

    python3 test/api_check.py --base-url http://127.0.0.1:8978 --phase main
    python3 test/api_check.py --base-url http://127.0.0.1:8978 --phase hot \
        --modules-dir /tmp/xxx/modules

输出每行 ``PASS <name>`` / ``FAIL <name> — <detail>`` / ``SKIP``，最后一行
``SUMMARY PASS=n FAIL=n SKIP=n``；有 FAIL 时退出码 1。

注意：本脚本**绝不**执行 genshin-touch 的中/高危险命令，也不会启动游戏。
"""

from __future__ import annotations

import argparse
import http.client
import json
import pathlib
import subprocess
import sys
import time
from typing import Any
from urllib.parse import urlparse

GENSHIN_DIR = "/home/h/dsh-plugins/.scratch/genshin-mobile"
GENSHIN_COMMANDS: dict[str, str] = {
    "play-inject": "./mobui-play.sh mobui30.dll",
    "ui-touch": "./mobui-ui.sh touch",
    "ui-pc": "./mobui-ui.sh pc",
    "ui-status": "./mobui-ui.sh status",
    "set-gain": "./mobui-gain.sh 35",
    "touchd-start": (
        "systemd-run --user --collect --unit=mobui-touchd "
        "--working-directory=/home/h/dsh-plugins/.scratch/genshin-mobile/tools "
        "/home/h/dsh-plugins/.scratch/genshin-mobile/tools/touchd auto 51721 1"
    ),
    "touchd-stop": "systemctl --user stop mobui-touchd",
    "verify-selftest": "bash verify-offline.sh --selftest",
    "perf-sample": "python3 tools/perf-sample.py 60 /tmp/perf.csv",
    "gpu-sample": "bash tools/gpu-sample.sh 60 /tmp/gpu.csv",
}
GENSHIN_RISKS: dict[str, str] = {
    "play-inject": "medium",
    "ui-touch": "low",
    "ui-pc": "low",
    "ui-status": "readonly",
    "set-gain": "low",
    "touchd-start": "low",
    "touchd-stop": "low",
    "verify-selftest": "low",
    "perf-sample": "readonly",
    "gpu-sample": "readonly",
}


def short(value: Any, limit: int = 220) -> str:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    text = text.replace("\n", "\\n")
    return text if len(text) <= limit else text[: limit - 1] + "…"


def header_value(headers: dict[str, str], name: str) -> str | None:
    for key, value in headers.items():
        if key.lower() == name.lower():
            return value
    return None


def game_running() -> bool:
    try:
        return subprocess.run(
            ["pgrep", "-x", "YuanShen.exe"], capture_output=True, check=False
        ).returncode == 0
    except FileNotFoundError:
        return False


class Prober:
    def __init__(self, base_url: str):
        parsed = urlparse(base_url)
        self.host = parsed.hostname or "127.0.0.1"
        self.port = parsed.port or 80
        self.base_url = base_url.rstrip("/")
        self.passed = 0
        self.failed = 0
        self.skipped = 0

    # ---- 结果记录 -----------------------------------------------------
    def check(self, name: str, condition: bool, detail: str = "") -> bool:
        if condition:
            print(f"PASS {name}" + (f" — {detail}" if detail else ""))
            self.passed += 1
        else:
            print(f"FAIL {name}" + (f" — {detail}" if detail else ""))
            self.failed += 1
        return bool(condition)

    def info(self, message: str) -> None:
        print(f"INFO {message}")

    def skip(self, name: str, reason: str) -> None:
        print(f"SKIP {name} — {reason}")
        self.skipped += 1

    # ---- 基础请求 -----------------------------------------------------
    def raw(
        self,
        method: str,
        path: str,
        body: Any = None,
        headers: dict[str, str] | None = None,
        csrf: bool = True,
        timeout: float = 30.0,
    ) -> tuple[int, bytes, dict[str, str]]:
        connection = http.client.HTTPConnection(self.host, self.port, timeout=timeout)
        request_headers: dict[str, str] = {}
        if csrf and method == "POST":
            request_headers["X-Requested-With"] = "cli-manager"
        request_headers.update(headers or {})
        payload: bytes | None = None
        if body is not None:
            if isinstance(body, (bytes, bytearray)):
                payload = bytes(body)
            else:
                payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
                request_headers.setdefault("Content-Type", "application/json")
        try:
            connection.request(method, path, body=payload, headers=request_headers)
            response = connection.getresponse()
            data = response.read()
            return response.status, data, dict(response.getheaders())
        finally:
            connection.close()

    def get_json(self, path: str) -> tuple[int, Any, dict[str, str]]:
        status, body, headers = self.raw("GET", path)
        try:
            data = json.loads(body.decode("utf-8")) if body else None
        except (UnicodeDecodeError, json.JSONDecodeError):
            data = None
        return status, data, headers

    def post_json(
        self,
        path: str,
        payload: Any,
        headers: dict[str, str] | None = None,
        csrf: bool = True,
    ) -> tuple[int, Any, dict[str, str]]:
        status, body, response_headers = self.raw(
            "POST", path, body=payload, headers=headers, csrf=csrf
        )
        try:
            data = json.loads(body.decode("utf-8")) if body else None
        except (UnicodeDecodeError, json.JSONDecodeError):
            data = None
        return status, data, response_headers

    def raw_with_host(self, path: str, host_header: str) -> int:
        connection = http.client.HTTPConnection(self.host, self.port, timeout=10)
        try:
            connection.putrequest("GET", path, skip_host=True, skip_accept_encoding=True)
            connection.putheader("Host", host_header)
            connection.endheaders()
            response = connection.getresponse()
            response.read()
            return response.status
        finally:
            connection.close()

    # ---- SSE ----------------------------------------------------------
    def sse(self, run_id: str, timeout: float = 60.0) -> list[dict[str, Any]]:
        connection = http.client.HTTPConnection(self.host, self.port, timeout=timeout)
        events: list[dict[str, Any]] = []
        try:
            connection.request(
                "GET",
                f"/api/runs/{run_id}/events?from=0",
                headers={"Accept": "text/event-stream"},
            )
            response = connection.getresponse()
            if response.status != 200:
                return events
            current: dict[str, Any] = {}
            started = time.monotonic()
            while True:
                line = response.readline()
                if not line:
                    break
                text = line.decode("utf-8", "replace").rstrip("\r\n")
                if text == "":
                    if current:
                        current["at"] = time.monotonic() - started
                        current["parsed"] = _parse_data(current.get("data"))
                        events.append(current)
                        current = {}
                    continue
                if text.startswith(":"):
                    continue
                field, _, value = text.partition(":")
                if value.startswith(" "):
                    value = value[1:]
                if field == "event":
                    current["event"] = value
                elif field == "data":
                    current["data"] = current.get("data", "") + value
                elif field == "id":
                    try:
                        current["id"] = int(value)
                    except ValueError:
                        pass
        finally:
            connection.close()
        return events

    # ---- 执行辅助 -----------------------------------------------------
    def run(
        self,
        module_id: str,
        command_id: str,
        params: dict[str, Any] | None = None,
        confirm: bool = False,
    ) -> tuple[int, Any, str | None, list[dict[str, Any]]]:
        payload: dict[str, Any] = {
            "moduleId": module_id,
            "commandId": command_id,
            "params": params or {},
        }
        if confirm:
            payload["confirm"] = True
        status, data, _ = self.post_json("/api/run", payload)
        if status != 200 or not isinstance(data, dict):
            return status, data, None, []
        run_id = data.get("runId")
        events = self.sse(run_id) if run_id else []
        return status, data, run_id, events


def _parse_data(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, str) or not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def stream_text(events: list[dict[str, Any]], channel: str) -> str:
    return "".join(
        str(event.get("parsed", {}).get("text", ""))
        for event in events
        if event.get("event") == channel
    )


def last_exit(events: list[dict[str, Any]]) -> dict[str, Any]:
    exits = [event["parsed"] for event in events if event.get("event") == "exit"]
    return exits[-1] if exits else {}


def event_types(events: list[dict[str, Any]]) -> list[str]:
    return [str(event.get("event")) for event in events]


# =====================================================================
# 主验收
# =====================================================================


def phase_main(prober: Prober) -> None:
    p = prober

    # ---- 健康检查 -----------------------------------------------------
    status, health, response_headers = p.get_json("/api/health")
    p.check("health.ok", status == 200 and isinstance(health, dict) and health.get("ok") is True, f"HTTP {status}")
    p.check(
        "health.no_cors",
        header_value(response_headers, "Access-Control-Allow-Origin") is None,
        "不应出现 CORS 放行头",
    )
    p.check("health.pid", isinstance(health, dict) and isinstance(health.get("pid"), int), short(health))

    # ---- 模块清单 -----------------------------------------------------
    status, data, _ = p.get_json("/api/modules")
    p.check("modules.http", status == 200 and isinstance(data, dict), f"HTTP {status}")
    modules = data.get("modules", []) if isinstance(data, dict) else []
    by_id = {module["id"]: module for module in modules}
    p.check("modules.count", len(modules) == 2, f"实得 {len(modules)}：{sorted(by_id)}")
    p.check("modules.errors.empty", data.get("errors") == [], short(data.get("errors")))
    p.check("modules.warnings.empty", data.get("warnings") == [], short(data.get("warnings")))
    p.check("modules.modulesDir", isinstance(data.get("modulesDir"), str) and data["modulesDir"].endswith("/modules"), str(data.get("modulesDir")))

    genshin = by_id.get("genshin-touch", {})
    p.check("genshin.present", bool(genshin))
    p.check("genshin.name", genshin.get("name") == "原神触摸/串流", str(genshin.get("name")))
    p.check("genshin.cwd", genshin.get("cwd") == GENSHIN_DIR, str(genshin.get("cwd")))
    p.check("genshin.cwd_exists", genshin.get("cwdExists") is True)
    p.check("genshin.notes", isinstance(genshin.get("notes"), list) and len(genshin["notes"]) >= 2, f"{len(genshin.get('notes', []))} 条提示")

    commands = {command["id"]: command for command in genshin.get("commands", [])}
    p.check("genshin.command_count", len(commands) == 10, f"实得 {len(commands)}")
    p.check("genshin.command_ids", set(commands) == set(GENSHIN_COMMANDS), f"缺 {sorted(set(GENSHIN_COMMANDS) - set(commands))} / 多 {sorted(set(commands) - set(GENSHIN_COMMANDS))}")

    for command_id, expected in GENSHIN_COMMANDS.items():
        command = commands.get(command_id, {})
        display = (command.get("preview") or {}).get("display")
        p.check(f"genshin.display.{command_id}", display == expected, f"实得 {short(display)}")
    for command_id, risk in GENSHIN_RISKS.items():
        command = commands.get(command_id, {})
        p.check(f"genshin.risk.{command_id}", command.get("risk") == risk, f"实得 {command.get('risk')}")
        p.check(
            f"genshin.confirm.{command_id}",
            command.get("needsConfirm") is (risk in {"medium", "high"}),
            f"risk={risk} needsConfirm={command.get('needsConfirm')}",
        )

    demo = by_id.get("demo", {})
    demo_commands = {command["id"]: command for command in demo.get("commands", [])}
    p.check("demo.present", bool(demo), str(demo.get("name")))
    p.check("demo.hello.display", (demo_commands.get("hello", {}).get("preview") or {}).get("display") == "echo hello")
    p.check("demo.confirm.needs", demo_commands.get("confirm-demo", {}).get("needsConfirm") is True)
    p.check("demo.stream.present", "stream-demo" in demo_commands)
    p.check("demo.count", len(demo_commands) >= 6, f"{len(demo_commands)} 条命令")

    # ---- 页面与静态资源 ----------------------------------------------
    status, body, headers = p.raw("GET", "/")
    html = body.decode("utf-8", "replace")
    p.check("page.status", status == 200, f"HTTP {status}")
    p.check("page.title", "CLI 命令管理器" in html)
    p.check("page.assets", "/static/app.js" in html and "/static/style.css" in html)
    p.check("page.csp", header_value(headers, "Content-Security-Policy") is not None)
    p.check("page.frame_deny", header_value(headers, "X-Frame-Options") == "DENY")
    p.check("page.no_inline_script", "<script>" not in html)

    status, js_body, js_headers = p.raw("GET", "/static/app.js")
    p.check(
        "static.js",
        status == 200 and "javascript" in (header_value(js_headers, "Content-Type") or ""),
        f"HTTP {status}",
    )
    status, css_body, _ = p.raw("GET", "/static/style.css")
    p.check("static.css", status == 200 and len(css_body) > 2000, f"{len(css_body)} 字节")
    status, _, _ = p.raw("GET", "/static/../cli_manager/config.py")
    p.check("static.traversal.blocked", status in (403, 404), f"HTTP {status}")
    status, _, _ = p.raw("HEAD", "/")
    p.check("page.head", status == 200, f"HTTP {status}")

    # ---- 渲染与参数校验 ----------------------------------------------
    status, rendered, _ = p.post_json(
        "/api/render",
        {"moduleId": "genshin-touch", "commandId": "set-gain", "params": {"gain": "40"}},
    )
    p.check(
        "render.gain.40",
        status == 200 and rendered.get("display") == "./mobui-gain.sh 40",
        short((rendered or {}).get("display")),
    )
    p.check(
        "render.gain.uses_argv",
        # set-gain 走脚本 argv（重定向搬进 mobui-gain.sh），不再用 shell 模板：
        # 模板里写死 build/mobui29.cmds 会在探针版本漂移时静默失效。
        status == 200 and rendered.get("usesShell") is False
        and rendered.get("argv") == ["./mobui-gain.sh", "40"],
        short((rendered or {}).get("argv")),
    )
    for label, params in [
        ("shell_injection", {"gain": "1; rm -rf /"}),
        ("nondigit", {"gain": "abc"}),
        ("newline", {"gain": "1\n2"}),
        ("unknown_param", {"nope": "1"}),
    ]:
        status, rendered, _ = p.post_json(
            "/api/render",
            {"moduleId": "genshin-touch", "commandId": "set-gain", "params": params},
        )
        p.check(f"render.reject.{label}", status == 400, f"HTTP {status} {short(rendered)}")

    for label, module_id, command_id, params in [
        ("port_injection", "genshin-touch", "touchd-start", {"port": "51721; reboot"}),
        ("arg_space", "demo", "greet", {"name": "a b"}),
        ("path_escape", "genshin-touch", "perf-sample", {"output": "/etc/passwd"}),
    ]:
        status, rendered, _ = p.post_json(
            "/api/render",
            {"moduleId": module_id, "commandId": command_id, "params": params},
        )
        p.check(f"render.reject.{label}", status == 400, f"HTTP {status} {short(rendered)}")

    # ---- 安全边界 -----------------------------------------------------
    status, _, _ = p.post_json("/api/run", {"moduleId": "demo", "commandId": "hello"}, csrf=False)
    p.check("security.csrf.missing_header", status == 403, f"HTTP {status}")
    status, _, _ = p.post_json(
        "/api/run",
        {"moduleId": "demo", "commandId": "hello"},
        headers={"Origin": "http://evil.example"},
    )
    p.check("security.csrf.bad_origin", status == 403, f"HTTP {status}")
    status, _, _ = p.post_json(
        "/api/run",
        {"moduleId": "demo", "commandId": "hello"},
        headers={"Content-Type": "text/plain"},
    )
    p.check("security.csrf.bad_content_type", status == 403, f"HTTP {status}")
    status = p.raw_with_host("/api/health", "evil.example")
    p.check("security.host.rejected", status == 403, f"HTTP {status}")

    # ---- 危险确认（后端硬门 + 无副作用） ----------------------------
    _, health_before, _ = p.get_json("/api/health")
    status, data, _ = p.post_json(
        "/api/run",
        {"moduleId": "genshin-touch", "commandId": "play-inject"},
    )
    p.check(
        "confirm.backend.required",
        status == 400 and data.get("code") == "confirm_required",
        f"HTTP {status} {short(data)}",
    )
    p.check(
        "confirm.backend.details",
        (data.get("details") or {}).get("display") == "./mobui-play.sh mobui30.dll",
        short((data or {}).get("details")),
    )
    _, health_after, _ = p.get_json("/api/health")
    p.check(
        "confirm.backend.no_side_effect",
        health_after.get("runCount") == health_before.get("runCount"),
        f"runCount {health_before.get('runCount')} → {health_after.get('runCount')}",
    )

    # ---- 真实执行：./mobui-ui.sh status（只读） ----------------------
    running = game_running()
    status, data, run_id, events = p.run("genshin-touch", "ui-status")
    exit_data = last_exit(events)
    p.check("run.ui_status.http", status == 200 and run_id is not None, f"HTTP {status} {short(data)}")
    p.check("run.ui_status.start_event", event_types(events)[:1] == ["start"], ",".join(event_types(events)[:3]))
    p.check("run.ui_status.exit_event", bool(exit_data), ",".join(event_types(events)))
    p.check(
        "run.ui_status.exit_code",
        exit_data.get("code") in (0, 3),
        f"code={exit_data.get('code')}（游戏{'运行中' if running else '未运行'}，按脚本逻辑应为 {0 if running else 3}）",
    )
    p.check(
        "run.ui_status.duration",
        isinstance(exit_data.get("durationMs"), int) and exit_data["durationMs"] >= 0,
        f"durationMs={exit_data.get('durationMs')}",
    )
    p.check(
        "run.ui_status.output",
        bool(stream_text(events, "stdout") or stream_text(events, "stderr")),
        f"stdout={len(stream_text(events, 'stdout'))}B stderr={len(stream_text(events, 'stderr'))}B",
    )
    if exit_data.get("code") == 3:
        p.check("run.ui_status.warning_when_stopped", "游戏没在跑" in stream_text(events, "stderr"), short(stream_text(events, "stderr")))
    elif exit_data.get("code") == 0:
        p.check("run.ui_status.no_warning_when_running", "游戏没在跑" not in stream_text(events, "stderr"), short(stream_text(events, "stderr")))
    p.check("run.ui_status.display", (data or {}).get("display") == "./mobui-ui.sh status", short((data or {}).get("display")))
    status, snapshot, _ = p.get_json(f"/api/runs/{run_id}")
    p.check(
        "run.snapshot",
        status == 200 and snapshot["run"]["status"] == "exited" and snapshot["run"]["exitCode"] == exit_data.get("code"),
        f"HTTP {status} {short((snapshot or {}).get('run'))}",
    )

    # ---- 非零退出码链路（与游戏状态无关） ----------------------------
    status, data, run_id, events = p.run("demo", "exit-3-demo")
    exit_data = last_exit(events)
    p.check("run.exit3.code", exit_data.get("code") == 3, f"code={exit_data.get('code')}")
    p.check("run.exit3.stdout", "这是标准输出" in stream_text(events, "stdout"), short(stream_text(events, "stdout")))
    p.check("run.exit3.stderr", "这是标准错误" in stream_text(events, "stderr"), short(stream_text(events, "stderr")))
    p.check(
        "run.exit3.duration",
        isinstance(exit_data.get("durationMs"), int) and exit_data["durationMs"] >= 0,
        f"durationMs={exit_data.get('durationMs')}",
    )

    # ---- 流式输出（真实时） ------------------------------------------
    status, data, run_id, events = p.run("demo", "stream-demo")
    stdout_events = [event for event in events if event.get("event") == "stdout"]
    exit_events = [event for event in events if event.get("event") == "exit"]
    exit_data = last_exit(events)
    p.check("stream.line_count", len(stdout_events) >= 5, f"{len(stdout_events)} 行")
    if len(stdout_events) >= 5:
        span = stdout_events[-1]["at"] - stdout_events[0]["at"]
        p.check("stream.incremental", span >= 0.75, f"首末行间隔 {span:.2f}s（应 ≈1.2s，证明不是结束后一次性返回）")
        exit_at = exit_events[-1]["at"] if exit_events else 0.0
        p.check("stream.exit_after_output", exit_at > stdout_events[-1]["at"], f"exit at {exit_at:.2f}s")
        p.check("stream.content", "流式第 5 行" in stream_text(events, "stdout"))
    p.check("stream.exit_code", exit_data.get("code") == 0, f"code={exit_data.get('code')}")

    # ---- 超时 ---------------------------------------------------------
    status, data, run_id, events = p.run("demo", "timeout-demo")
    exit_data = last_exit(events)
    p.check("timeout.timed_out_flag", exit_data.get("timedOut") is True, short(exit_data))
    p.check("timeout.status", exit_data.get("status") == "timed_out", str(exit_data.get("status")))
    duration = exit_data.get("durationMs")
    p.check(
        "timeout.duration",
        isinstance(duration, int) and 700 <= duration <= 6000,
        f"durationMs={duration}（timeoutSec=1）",
    )
    p.check("timeout.notice", "超时" in "".join(str(e.get("parsed", {}).get("message", "")) for e in events if e.get("event") == "notice"))

    # ---- 危险确认：确认后真的执行（demo 无害 echo） ------------------
    status, data, _ = p.post_json(
        "/api/run", {"moduleId": "demo", "commandId": "confirm-demo"}
    )
    p.check("confirm.demo.required", status == 400 and data.get("code") == "confirm_required", f"HTTP {status} {short(data)}")
    status, data, run_id, events = p.run("demo", "confirm-demo", confirm=True)
    exit_data = last_exit(events)
    p.check("confirm.demo.confirmed_run", status == 200 and exit_data.get("code") == 0, f"HTTP {status} code={exit_data.get('code')}")
    p.check("confirm.demo.output", "危险操作已确认" in stream_text(events, "stdout"), short(stream_text(events, "stdout")))

    # ---- 取消 ---------------------------------------------------------
    status, data, _ = p.post_json("/api/run", {"moduleId": "demo", "commandId": "sleep-demo"})
    run_id = (data or {}).get("runId")
    if status == 200 and run_id:
        time.sleep(0.3)
        cancel_status, cancel_data, _ = p.post_json(f"/api/runs/{run_id}/cancel", {})
        events = p.sse(run_id)
        exit_data = last_exit(events)
        p.check("cancel.request", cancel_status == 200 and cancel_data.get("ok") is True, f"HTTP {cancel_status} {short(cancel_data)}")
        p.check(
            "cancel.status",
            exit_data.get("status") == "cancelled" and exit_data.get("cancelled") is True,
            short(exit_data),
        )
        p.check(
            "cancel.early",
            isinstance(exit_data.get("durationMs"), int) and exit_data["durationMs"] < 5000,
            f"durationMs={exit_data.get('durationMs')}",
        )
    else:
        p.check("cancel.request", False, f"启动失败 HTTP {status} {short(data)}")

    # ---- 错误处理 -----------------------------------------------------
    status, data, _ = p.post_json("/api/run", {"moduleId": "nope", "commandId": "x"})
    p.check("errors.unknown_module", status == 404 and data.get("code") == "module_not_found", f"HTTP {status}")
    status, data, _ = p.post_json("/api/run", {"moduleId": "demo", "commandId": "nope"})
    p.check("errors.unknown_command", status == 404 and data.get("code") == "command_not_found", f"HTTP {status}")
    status, data, _ = p.post_json(
        "/api/run",
        {"moduleId": "genshin-touch", "commandId": "set-gain", "params": {"gain": "abc"}},
    )
    p.check("errors.invalid_params", status == 400 and data.get("code") == "invalid_params", f"HTTP {status}")
    status, data, _ = p.get_json("/api/runs/0123456789abcdef")
    p.check("errors.unknown_run", status == 404, f"HTTP {status}")
    status, body, _ = p.raw(
        "POST",
        "/api/run",
        body=b"{ not json",
        headers={"Content-Type": "application/json"},
    )
    p.check("errors.bad_json", status == 400, f"HTTP {status}")
    status, body, _ = p.raw("GET", "/api/nope")
    p.check("errors.unknown_api", status == 404, f"HTTP {status}")
    status, data, _ = p.post_json("/api/reload", {})
    p.check(
        "reload.ok",
        status == 200 and isinstance(data.get("modules"), list) and len(data["modules"]) == 2,
        f"HTTP {status}",
    )

    p.info(f"游戏进程当前{'在' if running else '不在'}运行——这决定 ui-status 的预期退出码（0 或 3），两者都是脚本正常行为。")


# =====================================================================
# 热加载 / 配置健壮性
# =====================================================================


def phase_hot(prober: Prober, modules_dir: str) -> None:
    p = prober
    directory = pathlib.Path(modules_dir)

    status, data, _ = p.get_json("/api/modules")
    baseline = {module["id"] for module in (data or {}).get("modules", [])}
    p.check("hot.baseline", status == 200 and {"genshin-touch", "demo"} <= baseline, f"实得 {sorted(baseline)}")

    hot_module = {
        "id": "hot-demo",
        "name": "热加载模块",
        "order": 50,
        "description": "测试运行期新增的模块",
        "cwd": "/tmp",
        "commands": [
            {
                "id": "hot-echo",
                "name": "热加载后的命令",
                "description": "echo hot-loaded",
                "risk": "readonly",
                "timeoutSec": 10,
                "argv": ["echo", "hot-loaded"],
            }
        ],
    }
    (directory / "hot-module.json").write_text(
        json.dumps(hot_module, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    time.sleep(0.05)
    status, data, _ = p.get_json("/api/modules")
    modules = {module["id"]: module for module in (data or {}).get("modules", [])}
    p.check("hot.auto_reload", status == 200 and "hot-demo" in modules, f"实得 {sorted(modules)}")
    hot = modules.get("hot-demo", {})
    p.check(
        "hot.preview",
        ((hot.get("commands") or [{}])[0].get("preview") or {}).get("display") == "echo hot-loaded",
        short(hot.get("commands")),
    )

    status, data, _ = p.post_json("/api/reload", {})
    p.check(
        "hot.explicit_reload",
        status == 200 and "hot-demo" in {module["id"] for module in (data or {}).get("modules", [])},
        f"HTTP {status}",
    )

    status, data, run_id, events = p.run("hot-demo", "hot-echo")
    exit_data = last_exit(events)
    p.check(
        "hot.run",
        status == 200 and exit_data.get("code") == 0 and "hot-loaded" in stream_text(events, "stdout"),
        f"HTTP {status} code={exit_data.get('code')}",
    )
    p.check("hot.run.events", event_types(events)[:1] == ["start"] and "exit" in event_types(events), ",".join(event_types(events)))

    (directory / "broken.json").write_text("{ 这不是 JSON", encoding="utf-8")
    time.sleep(0.05)
    status, data, _ = p.get_json("/api/modules")
    ids = {module["id"] for module in (data or {}).get("modules", [])}
    p.check(
        "hot.broken_isolated",
        status == 200 and bool(data.get("errors")) and {"genshin-touch", "hot-demo"} <= ids,
        f"errors={len(data.get('errors', []))} modules={sorted(ids)}",
    )

    bad_shell = {
        "id": "bad-shell",
        "name": "坏配置：shell 参数缺 pattern",
        "cwd": "/tmp",
        "commands": [
            {
                "id": "x",
                "name": "x",
                "risk": "low",
                "shell": "echo {{name}}",
                "params": [{"name": "name", "default": "a"}],
            }
        ],
    }
    (directory / "bad-shell.json").write_text(
        json.dumps(bad_shell, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    time.sleep(0.05)
    status, data, _ = p.get_json("/api/modules")
    p.check(
        "hot.bad_shell_rejected",
        any("pattern" in str(error.get("error", "")) for error in (data or {}).get("errors", [])),
        short((data or {}).get("errors")),
    )

    status, health, _ = p.get_json("/api/health")
    p.check("hot.health_after_errors", status == 200 and health.get("ok") is True, f"HTTP {status}")

    p.info("热加载阶段：新增模块 / 显式 reload / 坏 JSON 隔离 / shell 参数缺 pattern 拒绝，全部检查完成。")


# =====================================================================


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="dsh-cli-manager 机检脚本")
    parser.add_argument("--base-url", default="http://127.0.0.1:8977")
    parser.add_argument("--phase", choices=["main", "hot"], default="main")
    parser.add_argument("--modules-dir", default=None)
    args = parser.parse_args(argv)

    prober = Prober(args.base_url)
    print(f"# 目标 {prober.base_url}  phase={args.phase}")
    if args.phase == "main":
        phase_main(prober)
    else:
        if not args.modules_dir:
            print("phase=hot 需要 --modules-dir", file=sys.stderr)
            return 2
        phase_hot(prober, args.modules_dir)

    print(f"SUMMARY PASS={prober.passed} FAIL={prober.failed} SKIP={prober.skipped}")
    return 1 if prober.failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
