"""模块配置：加载、校验与命令渲染。

安全模型（必读）
----------------
1. 命令模板只来自本机 ``modules/*.json`` 配置文件，客户端永远不能提交命令字符串。
2. ``argv`` 形式不经过 shell；``shell`` 形式经 ``bash -c`` 执行。
3. 参数值只允许字符串，执行前按 ``pattern`` 全匹配校验；``shell`` 模板里引用的参数
   **必须**声明 ``pattern`` —— 这是防注入的硬边界。
4. 参数值一律拒绝 NUL / 换行 / 回车等控制字符。

新增一个项目的模块 = 往 ``modules/`` 目录放一个 JSON 文件，不需要改任何代码。
"""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import sys
import threading
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

RISK_LEVELS: dict[str, str] = {
    "readonly": "只读",
    "low": "低",
    "medium": "中",
    "high": "高",
}
#: 中/高危险级命令，后端强制要求 confirm: true
CONFIRM_RISKS = frozenset({"medium", "high"})

#: 命令归属平台；``all`` 表示两个平台都显示
PLATFORMS: tuple[str, ...] = ("linux", "windows", "all")
DEFAULT_PLATFORM = "all"
PLATFORM_LABELS: dict[str, str] = {"linux": "仅 Linux", "windows": "仅 Windows", "all": ""}

DEFAULT_TIMEOUT = 120
MAX_TIMEOUT = 1800
MAX_PARAMS = 16
CONTROL_CHARS = ("\x00", "\n", "\r")
# shell 模板参数的最小反注入探针：pattern 若连这些都放行，等于没有防护
SHELL_INJECTION_PROBES = (" ", "; id", "$(id)", "`id`", "| id", "&& id", "a'b", 'a"b')

MODULE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
COMMAND_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
PARAM_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,31}$")
TEMPLATE_RE = re.compile(r"\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}")
#: 环境变量引用；只展开 cwd / argv / env 的值，shell 模板交给 bash 运行时展开
VAR_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")

SHELL_PATH = shutil.which("bash") or "/bin/sh"


def current_platform() -> str:
    """当前平台；``CLI_MANAGER_PLATFORM`` 可覆盖（测试与跨平台预演用）。"""
    override = os.environ.get("CLI_MANAGER_PLATFORM", "").strip().lower()
    if override in ("linux", "windows"):
        return override
    return "windows" if sys.platform.startswith("win") else "linux"


def platform_available(platform: str) -> bool:
    return platform == DEFAULT_PLATFORM or platform == current_platform()


class ConfigError(Exception):
    """模块配置不合法。"""


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _log(message: str) -> None:
    print(f"[config] {message}", file=sys.stderr, flush=True)


# ---------------------------------------------------------------------------
# 数据结构
# ---------------------------------------------------------------------------


@dataclass
class Param:
    name: str
    label: str
    default: str
    pattern: str | None = None
    description: str = ""
    regex: re.Pattern[str] | None = field(default=None, repr=False, compare=False)

    def check(self, value: str) -> str:
        if not isinstance(value, str):
            raise ConfigError(f"参数 {self.name} 必须是字符串")
        for ch in CONTROL_CHARS:
            if ch in value:
                raise ConfigError(f"参数 {self.name} 含非法控制字符")
        if self.regex is not None and self.regex.fullmatch(value) is None:
            raise ConfigError(
                f"参数 {self.name} 不匹配 {self.pattern}（当前值：{value!r}）"
            )
        return value

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "label": self.label,
            "default": self.default,
            "pattern": self.pattern,
            "description": self.description,
        }


@dataclass
class Rendered:
    """一条命令在给定参数下、真正要执行的东西。"""

    display: str
    argv: list[str]
    uses_shell: bool
    params: dict[str, str]
    cwd: str
    env: dict[str, str]
    timeout_sec: int


@dataclass
class Command:
    id: str
    name: str
    description: str
    risk: str
    timeout_sec: int
    params: list[Param]
    shell: str | None
    argv: list[str] | None
    confirm_hint: str
    env: dict[str, str]
    platform: str = DEFAULT_PLATFORM
    module_id: str = ""
    module_name: str = ""
    module: "Module | None" = None
    default_preview: Rendered | None = None

    @property
    def needs_confirm(self) -> bool:
        return self.risk in CONFIRM_RISKS

    def render(self, values: dict[str, Any] | None = None) -> Rendered:
        """把参数套进模板，返回要执行的 argv 与给人看的命令文本。

        参数不合法时抛 :class:`ConfigError`（HTTP 层转 400）。
        """
        values = {} if values is None else values
        if not isinstance(values, dict):
            raise ConfigError("params 必须是 JSON 对象")
        known = {p.name for p in self.params}
        unknown = sorted(str(k) for k in values if k not in known)
        if unknown:
            raise ConfigError("未知参数：" + "、".join(unknown))

        resolved: dict[str, str] = {}
        for p in self.params:
            raw = values.get(p.name, p.default)
            if raw is None:
                raw = p.default
            if not isinstance(raw, str):
                raise ConfigError(f"参数 {p.name} 必须是字符串")
            resolved[p.name] = p.check(raw)

        if self.shell is not None:
            display = TEMPLATE_RE.sub(lambda m: resolved[m.group(1)], self.shell)
            argv = [SHELL_PATH, "-c", display]
            uses_shell = True
        else:
            assert self.argv is not None
            argv = [TEMPLATE_RE.sub(lambda m: resolved[m.group(1)], item) for item in self.argv]
            display = shlex.join(argv)
            uses_shell = False

        cwd = self.module.cwd if self.module is not None else ""
        return Rendered(
            display=display,
            argv=argv,
            uses_shell=uses_shell,
            params=resolved,
            cwd=cwd,
            env=dict(self.env),
            timeout_sec=self.timeout_sec,
        )

    def preview_dict(self) -> dict[str, Any]:
        preview = self.default_preview
        return {
            "display": preview.display if preview else "",
            "argv": list(preview.argv) if preview else [],
            "usesShell": preview.uses_shell if preview else False,
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "risk": self.risk,
            "riskLabel": RISK_LEVELS[self.risk],
            "needsConfirm": self.needs_confirm,
            "confirmHint": self.confirm_hint,
            "timeoutSec": self.timeout_sec,
            "executor": "shell" if self.shell is not None else "argv",
            "platform": self.platform,
            "platformLabel": PLATFORM_LABELS.get(self.platform, self.platform),
            "platformAvailable": platform_available(self.platform),
            "params": [p.to_dict() for p in self.params],
            "preview": self.preview_dict(),
        }


@dataclass
class Module:
    id: str
    name: str
    description: str
    cwd_raw: str
    cwd: str
    cwd_exists: bool
    notes: list[str]
    source: str
    order: int
    commands: list[Command] = field(default_factory=list)

    def find_command(self, command_id: str) -> Command | None:
        for command in self.commands:
            if command.id == command_id:
                return command
        return None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "cwd": self.cwd,
            "cwdRaw": self.cwd_raw,
            "cwdExists": self.cwd_exists,
            "notes": list(self.notes),
            "source": self.source,
            "order": self.order,
            "commands": [c.to_dict() for c in self.commands],
        }


@dataclass
class LoadResult:
    modules: list[Module]
    errors: list[dict[str, str]]
    warnings: list[dict[str, str]]
    loaded_at: str
    signature: tuple


# ---------------------------------------------------------------------------
# 解析
# ---------------------------------------------------------------------------


def _expand_vars(text: str, where: str, warnings: list[dict[str, str]]) -> str:
    """展开 ``${VAR}``；未定义的变量保留原文并记一条告警（同一来源只报一次）。"""
    missing: list[str] = []

    def repl(match: re.Match[str]) -> str:
        name = match.group(1)
        value = os.environ.get(name)
        if value is None:
            missing.append(name)
            return match.group(0)
        return value

    out = VAR_RE.sub(repl, text)
    if missing:
        warnings.append(
            {"source": where, "message": f"未定义环境变量：{'、'.join(sorted(set(missing)))}（保留原文）"}
        )
    return out


def _require_str(value: Any, field_name: str) -> str:
    if not isinstance(value, str):
        raise ConfigError(f"字段 {field_name} 必须是字符串")
    return value


def _parse_param(raw: Any, command_id: str, index: int) -> Param:
    if not isinstance(raw, dict):
        raise ConfigError(f"命令 {command_id} 的 params[{index}] 必须是对象")
    name = _require_str(raw.get("name", ""), f"{command_id}.params[{index}].name").strip()
    if not PARAM_NAME_RE.fullmatch(name):
        raise ConfigError(
            f"命令 {command_id} 的参数名不合法：{name!r}（只允许字母/数字/下划线，字母开头）"
        )
    label = raw.get("label", name)
    if not isinstance(label, str):
        raise ConfigError(f"命令 {command_id} 参数 {name} 的 label 必须是字符串")
    default = raw.get("default", "")
    if not isinstance(default, str):
        if isinstance(default, (int, float)) and not isinstance(default, bool):
            default = str(default)
        else:
            raise ConfigError(f"命令 {command_id} 参数 {name} 的 default 必须是字符串")
    pattern = raw.get("pattern")
    regex = None
    if pattern is not None:
        if not isinstance(pattern, str) or not pattern:
            raise ConfigError(f"命令 {command_id} 参数 {name} 的 pattern 必须是非空字符串")
        try:
            regex = re.compile(pattern)
        except re.error as exc:
            raise ConfigError(f"命令 {command_id} 参数 {name} 的 pattern 无法编译：{exc}") from exc
    description = raw.get("description", "")
    if not isinstance(description, str):
        raise ConfigError(f"命令 {command_id} 参数 {name} 的 description 必须是字符串")
    param = Param(
        name=name,
        label=label,
        default=default,
        pattern=pattern,
        description=description,
        regex=regex,
    )
    # 默认值必须自身合法，避免 UI 第一屏就报错
    param.check(default)
    return param


def _parse_command(
    raw: Any, source: str, index: int, default_platform: str = DEFAULT_PLATFORM
) -> tuple[Command, list[dict[str, str]]]:
    where = f"{source} commands[{index}]"
    if not isinstance(raw, dict):
        raise ConfigError(f"{where} 必须是对象")

    command_id = _require_str(raw.get("id", ""), f"{where}.id").strip()
    if not COMMAND_ID_RE.fullmatch(command_id):
        raise ConfigError(f"{where} 的 id 不合法：{command_id!r}（小写字母/数字/._-）")
    name = _require_str(raw.get("name", ""), f"{where}.name").strip()
    if not name:
        raise ConfigError(f"{where} 的 name 不能为空")

    description = raw.get("description", "")
    if not isinstance(description, str):
        raise ConfigError(f"{where} 的 description 必须是字符串")

    risk = raw.get("risk")
    if not isinstance(risk, str) or risk not in RISK_LEVELS:
        raise ConfigError(
            f"{where} 的 risk 必须是 {' / '.join(RISK_LEVELS)} 之一（当前：{risk!r}）"
        )

    timeout = raw.get("timeoutSec", DEFAULT_TIMEOUT)
    if isinstance(timeout, bool) or not isinstance(timeout, int) or not (1 <= timeout <= MAX_TIMEOUT):
        raise ConfigError(f"{where} 的 timeoutSec 必须是 1..{MAX_TIMEOUT} 的整数")

    shell = raw.get("shell")
    argv = raw.get("argv")
    if (shell is None) == (argv is None):
        raise ConfigError(f"{where} 必须且只能提供 shell 或 argv 之一")
    if shell is not None:
        if not isinstance(shell, str) or not shell.strip():
            raise ConfigError(f"{where} 的 shell 必须是非空字符串")
        shell = shell.strip()
    if argv is not None:
        if (
            not isinstance(argv, list)
            or not argv
            or not all(isinstance(item, str) and item != "" for item in argv)
        ):
            raise ConfigError(f"{where} 的 argv 必须是非空字符串数组")

    params_raw = raw.get("params", [])
    if not isinstance(params_raw, list):
        raise ConfigError(f"{where} 的 params 必须是数组")
    if len(params_raw) > MAX_PARAMS:
        raise ConfigError(f"{where} 的参数不能超过 {MAX_PARAMS} 个")
    params: list[Param] = []
    seen: set[str] = set()
    for j, item in enumerate(params_raw):
        param = _parse_param(item, command_id, j)
        if param.name in seen:
            raise ConfigError(f"{where} 参数名重复：{param.name}")
        seen.add(param.name)
        params.append(param)

    template_text = shell if shell is not None else "\n".join(argv or [])
    referenced = set(TEMPLATE_RE.findall(template_text))
    declared = {p.name for p in params}
    missing = sorted(referenced - declared)
    if missing:
        raise ConfigError(f"{where} 模板引用了未声明参数：{'、'.join(missing)}")
    if shell is not None:
        for pname in sorted(referenced):
            param = next(p for p in params if p.name == pname)
            if param.regex is None:
                raise ConfigError(
                    f"{where} 的 shell 模板引用了参数 {pname}，该参数必须声明 pattern（防注入）"
                )
            too_permissive = [
                probe for probe in SHELL_INJECTION_PROBES if param.regex.fullmatch(probe)
            ]
            if too_permissive:
                raise ConfigError(
                    f"{where} 的参数 {pname} 会出现在 shell 模板中，但 pattern 过于宽松"
                    f"（放行了 {too_permissive[0]!r} 等 shell 元字符/空格）；"
                    "请收紧 pattern，或改用 argv 数组"
                )

    warnings: list[dict[str, str]] = []
    unused = sorted(declared - referenced)
    if unused:
        warnings.append(
            {"source": source, "message": f"命令 {command_id} 的参数未在模板中使用：{'、'.join(unused)}"}
        )

    platform = raw.get("platform", default_platform)
    if not isinstance(platform, str) or platform not in PLATFORMS:
        raise ConfigError(
            f"{where} 的 platform 必须是 {' / '.join(PLATFORMS)} 之一（当前：{platform!r}）"
        )

    confirm_hint = raw.get("confirmHint", "")
    if not isinstance(confirm_hint, str):
        raise ConfigError(f"{where} 的 confirmHint 必须是字符串")

    env_raw = raw.get("env", {})
    if not isinstance(env_raw, dict):
        raise ConfigError(f"{where} 的 env 必须是对象")
    env: dict[str, str] = {}
    for key, value in env_raw.items():
        if not isinstance(key, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
            raise ConfigError(f"{where} 的 env 变量名不合法：{key!r}")
        if not isinstance(value, str) or any(ch in value for ch in CONTROL_CHARS):
            raise ConfigError(f"{where} 的 env.{key} 必须是不含控制字符的字符串")
        env[key] = _expand_vars(value, f"{where}.env", warnings)

    if argv is not None:
        argv = [_expand_vars(item, f"{where}.argv", warnings) for item in argv]

    command = Command(
        id=command_id,
        name=name,
        description=description,
        risk=risk,
        timeout_sec=timeout,
        params=params,
        shell=shell,
        argv=list(argv) if argv is not None else None,
        confirm_hint=confirm_hint,
        env=env,
        platform=platform,
    )
    return command, warnings


def _parse_module(raw: Any, source: str, app_root: str, path: str) -> tuple[Module, list[dict[str, str]]]:
    if not isinstance(raw, dict):
        raise ConfigError("模块文件必须是 JSON 对象")
    warnings: list[dict[str, str]] = []
    known_top = {"id", "name", "description", "cwd", "notes", "order", "commands", "platform"}
    for key in raw:
        if key not in known_top:
            warnings.append({"source": source, "message": f"未知字段 {key}（已忽略）"})

    module_id = raw.get("id") or os.path.splitext(os.path.basename(path))[0]
    if not isinstance(module_id, str):
        raise ConfigError("模块 id 必须是字符串")
    module_id = module_id.strip()
    if not MODULE_ID_RE.fullmatch(module_id):
        raise ConfigError(f"模块 id 不合法：{module_id!r}（小写字母/数字/._-）")

    name = raw.get("name") or module_id
    if not isinstance(name, str) or not name.strip():
        raise ConfigError("模块 name 不能为空")
    name = name.strip()

    description = raw.get("description", "")
    if not isinstance(description, str):
        raise ConfigError("模块 description 必须是字符串")

    cwd_raw = raw.get("cwd", ".")
    if not isinstance(cwd_raw, str) or not cwd_raw.strip():
        raise ConfigError("模块 cwd 必须是非空字符串")
    cwd_raw = cwd_raw.strip()
    cwd_raw = _expand_vars(cwd_raw, f"{source}.cwd", warnings)
    expanded = os.path.expanduser(cwd_raw)
    if os.path.isabs(expanded):
        cwd = os.path.abspath(expanded)
    else:
        cwd = os.path.abspath(os.path.join(app_root, expanded))

    notes_raw = raw.get("notes", [])
    if isinstance(notes_raw, str):
        notes_raw = [notes_raw]
    if not isinstance(notes_raw, list) or not all(isinstance(n, str) for n in notes_raw):
        raise ConfigError("模块 notes 必须是字符串或字符串数组")
    notes = [n for n in (n.strip() for n in notes_raw) if n]

    order = raw.get("order", 100)
    if isinstance(order, bool) or not isinstance(order, int):
        raise ConfigError("模块 order 必须是整数")

    default_platform = raw.get("platform", DEFAULT_PLATFORM)
    if not isinstance(default_platform, str) or default_platform not in PLATFORMS:
        raise ConfigError(
            f"模块 platform 必须是 {' / '.join(PLATFORMS)} 之一（当前：{default_platform!r}）"
        )

    commands_raw = raw.get("commands")
    if not isinstance(commands_raw, list) or not commands_raw:
        raise ConfigError("模块 commands 必须是非空数组")
    commands: list[Command] = []
    seen: set[str] = set()
    for i, item in enumerate(commands_raw):
        command, cmd_warnings = _parse_command(item, source, i, default_platform)
        if command.id in seen:
            raise ConfigError(f"命令 id 重复：{command.id}")
        seen.add(command.id)
        commands.append(command)
        warnings.extend(cmd_warnings)

    module = Module(
        id=module_id,
        name=name,
        description=description,
        cwd_raw=cwd_raw,
        cwd=cwd,
        cwd_exists=os.path.isdir(cwd),
        notes=notes,
        source=source,
        order=order,
        commands=commands,
    )
    for command in module.commands:
        command.module = module
        command.module_id = module.id
        command.module_name = module.name
        command.default_preview = command.render({})
    return module, warnings


# ---------------------------------------------------------------------------
# 加载器
# ---------------------------------------------------------------------------


class ModuleLoader:
    """加载 ``modules/*.json``；目录内容变化时自动重载。"""

    def __init__(self, modules_dir: str, app_root: str):
        self.modules_dir = os.path.abspath(modules_dir)
        self.app_root = os.path.abspath(app_root)
        self._lock = threading.Lock()
        self._result = LoadResult([], [], [], now_iso(), ())
        self.reload_count = 0
        self.load(force=True)

    # -- 签名：文件名 + mtime + size，任意变化触发重载 -----------------
    def signature(self) -> tuple:
        try:
            names = sorted(os.listdir(self.modules_dir))
        except OSError:
            return (("__missing__", self.modules_dir),)
        sig: list[tuple] = []
        for name in names:
            if not name.endswith(".json") or name.startswith("."):
                continue
            path = os.path.join(self.modules_dir, name)
            try:
                st = os.stat(path)
            except OSError:
                continue
            sig.append((name, st.st_mtime_ns, st.st_size))
        return tuple(sig)

    def ensure_fresh(self) -> bool:
        with self._lock:
            if self.signature() == self._result.signature:
                return False
            self._load_locked()
            return True

    def load(self, force: bool = False) -> LoadResult:
        with self._lock:
            if force or self.signature() != self._result.signature:
                self._load_locked()
            return self._result

    def _load_locked(self) -> None:
        sig = self.signature()
        modules: list[Module] = []
        errors: list[dict[str, str]] = []
        warnings: list[dict[str, str]] = []
        seen_ids: set[str] = set()

        try:
            names = sorted(
                n
                for n in os.listdir(self.modules_dir)
                if n.endswith(".json") and not n.startswith(".")
            )
        except OSError as exc:
            names = []
            errors.append({"source": self.modules_dir, "error": f"模块目录不可读：{exc}"})

        for name in names:
            path = os.path.join(self.modules_dir, name)
            source = os.path.relpath(path, self.app_root) if path.startswith(self.app_root + os.sep) else path
            try:
                with open(path, "r", encoding="utf-8") as fh:
                    raw = json.load(fh)
                module, mod_warnings = _parse_module(raw, source, self.app_root, path)
                if module.id in seen_ids:
                    raise ConfigError(f"模块 id 重复：{module.id}（{source}）")
                seen_ids.add(module.id)
                modules.append(module)
                warnings.extend(mod_warnings)
                _log(
                    f"加载模块 {module.name}（{module.id}）：{len(module.commands)} 条命令"
                    f" cwd={module.cwd}{'' if module.cwd_exists else ' [目录不存在]'}"
                )
            except (json.JSONDecodeError, UnicodeDecodeError, ConfigError, OSError) as exc:
                errors.append({"source": source, "error": str(exc)})
                _log(f"模块加载失败 {source}：{exc}")

        modules.sort(key=lambda m: (m.order, m.name, m.id))
        self._result = LoadResult(modules, errors, warnings, now_iso(), sig)
        self.reload_count += 1
        for warning in warnings:
            _log(f"配置警告 {warning['source']}：{warning['message']}")
        _log(
            f"配置加载完成：{len(modules)} 个模块 / "
            f"{sum(len(m.commands) for m in modules)} 条命令 / {len(errors)} 个错误"
        )

    def find_module(self, module_id: str) -> Module | None:
        for module in self._result.modules:
            if module.id == module_id:
                return module
        return None

    def snapshot(self) -> dict[str, Any]:
        self.ensure_fresh()
        with self._lock:
            result = self._result
            return {
                "modules": [m.to_dict() for m in result.modules],
                "errors": list(result.errors),
                "warnings": list(result.warnings),
                "loadedAt": result.loaded_at,
                "reloadCount": self.reload_count,
                "modulesDir": self.modules_dir,
            }

    def describe(self) -> str:
        """给 ``--check`` 用的人类可读摘要。"""
        self.ensure_fresh()
        with self._lock:
            result = self._result
            lines = [f"模块目录: {self.modules_dir}"]
            for module in result.modules:
                missing = "" if module.cwd_exists else f"  [cwd 不存在: {module.cwd}]"
                lines.append(
                    f"✓ {module.name}（{module.id}）：{len(module.commands)} 条命令，"
                    f"cwd={module.cwd}{missing}  ← {module.source}"
                )
            for warning in result.warnings:
                lines.append(f"! 警告 {warning['source']}：{warning['message']}")
            for error in result.errors:
                lines.append(f"✗ 错误 {error['source']}：{error['error']}")
            lines.append(
                f"合计：{len(result.modules)} 个模块 / "
                f"{sum(len(m.commands) for m in result.modules)} 条命令 / "
                f"{len(result.errors)} 个错误 / {len(result.warnings)} 个警告"
            )
            return "\n".join(lines)

    @property
    def errors(self) -> list[dict[str, str]]:
        return list(self._result.errors)
