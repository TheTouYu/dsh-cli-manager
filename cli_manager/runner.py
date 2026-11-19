"""命令执行器：子进程 + 流式事件 + 超时/取消。

每个 Run 维护一条事件流（start / stdout / stderr / pid / notice / exit），
HTTP 层的 SSE 端点只是把它转成 ``text/event-stream``。事件序号从 0 递增，
便于断线后按 ``Last-Event-ID`` 续传。
"""

from __future__ import annotations

import os
import secrets
import signal
import subprocess
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from .config import Command, Module, Rendered, now_iso

MAX_OUTPUT_CHARS = 1_000_000
MAX_RUNS = 50
TERMINAL_STATUSES = frozenset({"exited", "timed_out", "cancelled", "error"})


@dataclass
class Event:
    seq: int
    type: str
    data: dict[str, Any]
    ts: float

    def to_dict(self) -> dict[str, Any]:
        return {"seq": self.seq, "type": self.type, "data": self.data, "ts": self.ts}


class Run:
    """一次命令执行。线程安全：事件读写都在 ``self.cond`` 下完成。"""

    def __init__(self, run_id: str, module: Module, command: Command, rendered: Rendered):
        self.run_id = run_id
        self.module_id = module.id
        self.module_name = module.name
        self.command_id = command.id
        self.command_name = command.name
        self.rendered = rendered
        self.cwd = module.cwd
        self.timeout_sec = rendered.timeout_sec

        self.events: list[Event] = []
        self.cond = threading.Condition()
        self.status = "starting"
        self.exit_code: int | None = None
        self.duration_ms: int | None = None
        self.timed_out = False
        self.cancelled = False
        self.error: str | None = None
        self.started_at = now_iso()
        self.finished_at: str | None = None
        self.proc: subprocess.Popen[str] | None = None
        self.thread: threading.Thread | None = None

        self._cancel_requested = False
        self._closed = False
        self._chars = 0
        self._overflow = False

    # -- 事件 -----------------------------------------------------------
    def publish(self, event_type: str, data: dict[str, Any]) -> None:
        with self.cond:
            event = Event(len(self.events), event_type, data, time.monotonic())
            self.events.append(event)
            self.cond.notify_all()

    def wait_events(self, from_index: int, timeout: float) -> tuple[list[Event], bool]:
        """返回 ``(新事件, 是否已结束)``；没有新事件时最多等待 ``timeout`` 秒。"""
        with self.cond:
            if from_index >= len(self.events) and not self._closed:
                self.cond.wait(timeout)
            events = self.events[from_index:]
            return events, self._closed

    # -- 状态 -----------------------------------------------------------
    def is_finished(self) -> bool:
        with self.cond:
            return self._closed

    def snapshot(self) -> dict[str, Any]:
        with self.cond:
            return {
                "runId": self.run_id,
                "moduleId": self.module_id,
                "moduleName": self.module_name,
                "commandId": self.command_id,
                "commandName": self.command_name,
                "display": self.rendered.display,
                "argv": list(self.rendered.argv),
                "usesShell": self.rendered.uses_shell,
                "cwd": self.cwd,
                "timeoutSec": self.timeout_sec,
                "status": self.status,
                "exitCode": self.exit_code,
                "durationMs": self.duration_ms,
                "timedOut": self.timed_out,
                "cancelled": self.cancelled,
                "error": self.error,
                "startedAt": self.started_at,
                "finishedAt": self.finished_at,
                "eventCount": len(self.events),
            }

    def events_snapshot(self, limit: int = 5000) -> tuple[list[dict[str, Any]], bool]:
        with self.cond:
            events = self.events[-limit:]
            truncated = len(self.events) > limit
            return [e.to_dict() for e in events], truncated

    # -- 控制 -----------------------------------------------------------
    def request_cancel(self) -> bool:
        with self.cond:
            if self._closed:
                return False
            self._cancel_requested = True
        if self.proc is not None and self.proc.poll() is None:
            self._kill_group(signal.SIGTERM)
        return True

    def start(self) -> None:
        self.thread = threading.Thread(
            target=self._pump, name=f"cli-run-{self.run_id}", daemon=True
        )
        self.thread.start()

    # -- 内部 -----------------------------------------------------------
    def _publish_start(self) -> None:
        self.publish(
            "start",
            {
                "runId": self.run_id,
                "moduleId": self.module_id,
                "moduleName": self.module_name,
                "commandId": self.command_id,
                "commandName": self.command_name,
                "display": self.rendered.display,
                "argv": list(self.rendered.argv),
                "usesShell": self.rendered.uses_shell,
                "cwd": self.cwd,
                "timeoutSec": self.timeout_sec,
                "startedAt": self.started_at,
            },
        )

    def _pump(self) -> None:
        started = time.monotonic()
        self._publish_start()
        self.status = "running"

        if not os.path.isdir(self.cwd):
            self._finish(
                started,
                code=None,
                status="error",
                error=f"工作目录不存在：{self.cwd}",
            )
            return

        env = os.environ.copy()
        env.update(self.rendered.env)
        try:
            proc = subprocess.Popen(
                self.rendered.argv,
                cwd=self.cwd,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                start_new_session=True,
                env=env,
                close_fds=True,
            )
        except OSError as exc:
            self.publish("notice", {"level": "error", "message": f"启动失败：{exc}"})
            self._finish(started, code=None, status="error", error=f"启动失败：{exc}")
            return

        self.proc = proc
        self.publish("pid", {"pid": proc.pid})

        readers = [
            threading.Thread(
                target=self._read_stream, args=(proc.stdout, "stdout"), daemon=True
            ),
            threading.Thread(
                target=self._read_stream, args=(proc.stderr, "stderr"), daemon=True
            ),
        ]
        for reader in readers:
            reader.start()

        timed_out = False
        deadline = started + self.timeout_sec
        while True:
            try:
                proc.wait(timeout=0.1)
                break
            except subprocess.TimeoutExpired:
                pass
            with self.cond:
                want_cancel = self._cancel_requested
            if want_cancel:
                self._terminate(proc, grace=2.5)
                break
            if time.monotonic() >= deadline:
                timed_out = True
                self.publish("notice", {"level": "warn", "message": f"超过 {self.timeout_sec}s 超时，正在终止进程组"})
                self._terminate(proc, grace=2.0)
                break

        code = proc.poll()
        if code is None:  # 极端情况：TERM/KILL 后仍未回收
            self._kill_group(signal.SIGKILL)
            try:
                code = proc.wait(timeout=3.0)
            except subprocess.TimeoutExpired:
                code = None

        for reader in readers:
            reader.join(timeout=3.0)

        with self.cond:
            cancelled = self._cancel_requested and not timed_out
        if timed_out:
            status = "timed_out"
        elif cancelled:
            status = "cancelled"
        else:
            status = "exited"
        self._finish(started, code=code, status=status, timed_out=timed_out, cancelled=cancelled)

    def _read_stream(self, pipe, channel: str) -> None:
        if pipe is None:
            return
        try:
            for line in iter(pipe.readline, ""):
                if self._overflow:
                    continue
                with self.cond:
                    self._chars += len(line)
                    overflow_now = self._chars > MAX_OUTPUT_CHARS
                    if overflow_now:
                        self._overflow = True
                self.publish(channel, {"text": line})
                if overflow_now:
                    self.publish(
                        "notice",
                        {
                            "level": "warn",
                            "message": f"输出超过 {MAX_OUTPUT_CHARS} 字符上限，后续内容已丢弃",
                        },
                    )
        except (ValueError, OSError):
            pass
        finally:
            try:
                pipe.close()
            except OSError:
                pass

    def _finish(
        self,
        started: float,
        *,
        code: int | None,
        status: str,
        timed_out: bool = False,
        cancelled: bool = False,
        error: str | None = None,
    ) -> None:
        duration_ms = int(round((time.monotonic() - started) * 1000))
        with self.cond:
            self.status = status
            self.exit_code = code
            self.duration_ms = duration_ms
            self.timed_out = timed_out
            self.cancelled = cancelled
            self.error = error
            self.finished_at = now_iso()
            self.events.append(
                Event(
                    len(self.events),
                    "exit",
                    {
                        "code": code,
                        "durationMs": duration_ms,
                        "status": status,
                        "timedOut": timed_out,
                        "cancelled": cancelled,
                        "error": error,
                    },
                    time.monotonic(),
                )
            )
            self._closed = True
            self.cond.notify_all()

    def _kill_group(self, sig: int) -> None:
        proc = self.proc
        if proc is None or proc.poll() is not None:
            return
        try:
            os.killpg(os.getpgid(proc.pid), sig)
        except (ProcessLookupError, PermissionError):
            try:
                proc.send_signal(sig)
            except (ProcessLookupError, OSError):
                pass

    def _terminate(self, proc: subprocess.Popen[str], grace: float) -> None:
        self._kill_group(signal.SIGTERM)
        try:
            proc.wait(timeout=grace)
            return
        except subprocess.TimeoutExpired:
            pass
        self._kill_group(signal.SIGKILL)
        try:
            proc.wait(timeout=3.0)
        except subprocess.TimeoutExpired:
            pass


class RunRegistry:
    """内存中的运行记录（最多保留 ``max_runs`` 条，优先淘汰已结束的）。"""

    def __init__(self, max_runs: int = MAX_RUNS):
        self._lock = threading.Lock()
        self._runs: dict[str, Run] = {}
        self.max_runs = max_runs

    def create(self, module: Module, command: Command, rendered: Rendered) -> Run:
        run_id = secrets.token_hex(8)
        run = Run(run_id, module, command, rendered)
        with self._lock:
            self._runs[run_id] = run
            self._evict_locked()
        run.start()
        return run

    def get(self, run_id: str) -> Run | None:
        with self._lock:
            return self._runs.get(run_id)

    def count(self) -> int:
        with self._lock:
            return len(self._runs)

    def _evict_locked(self) -> None:
        if len(self._runs) <= self.max_runs:
            return
        for run_id in list(self._runs):
            if len(self._runs) <= self.max_runs:
                break
            if self._runs[run_id].is_finished():
                del self._runs[run_id]
