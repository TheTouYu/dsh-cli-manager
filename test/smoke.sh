#!/usr/bin/env bash
# dsh-cli-manager 一键机检：起服务 → curl/HTML/DOM → API/SSE/危险确认 → 热加载模块
#
# 用法:
#   ./test/smoke.sh
#   SMOKE_PORT=8990 ./test/smoke.sh      # 指定起始端口
#
# 输出 PASS/FAIL/SKIP 明细与最后一行汇总；有 FAIL 时退出码 1。
set -uo pipefail

ROOT="$(cd "$(dirname "$(readlink -f "$0")")/.." && pwd)"
TMP="$(mktemp -d "${TMPDIR:-/tmp}/cli-manager-smoke.XXXXXX")"
RESULT_LOG="$TMP/results.log"
: >"$RESULT_LOG"
SERVER_PID=""
SERVER_PORT=""

say() { printf '%s\n' "$*" | tee -a "$RESULT_LOG"; }
pass() { say "PASS $1${2:+ — $2}"; }
fail() { say "FAIL $1${2:+ — $2}"; }
skip() { say "SKIP $1${2:+ — $2}"; }
info() { say "INFO $*"; }

stop_server() {
  if [[ -n "${SERVER_PID:-}" ]] && kill -0 "$SERVER_PID" 2>/dev/null; then
    kill "$SERVER_PID" 2>/dev/null || true
    for _ in $(seq 1 50); do
      kill -0 "$SERVER_PID" 2>/dev/null || break
      sleep 0.1
    done
    kill -9 "$SERVER_PID" 2>/dev/null || true
    wait "$SERVER_PID" 2>/dev/null || true
  fi
  SERVER_PID=""
  SERVER_PORT=""
}

cleanup() {
  stop_server >/dev/null 2>&1 || true
  rm -rf "$TMP"
}
trap cleanup EXIT

finalize() {
  local pass_count fail_count skip_count
  pass_count="$(grep -c '^PASS ' "$RESULT_LOG" 2>/dev/null || true)"
  fail_count="$(grep -c '^FAIL ' "$RESULT_LOG" 2>/dev/null || true)"
  skip_count="$(grep -c '^SKIP ' "$RESULT_LOG" 2>/dev/null || true)"
  pass_count="${pass_count:-0}"
  fail_count="${fail_count:-0}"
  skip_count="${skip_count:-0}"
  say "=============================================================="
  say "SMOKE 汇总：PASS=$pass_count FAIL=$fail_count SKIP=$skip_count"
  if [[ "$fail_count" -gt 0 ]]; then
    say "结果：失败 —— 先看上面 FAIL 行的 detail"
    exit 1
  fi
  say "结果：全部可机检项通过"
  exit 0
}

find_free_port() {
  python3 - "$1" <<'PY'
import socket
import sys

start = int(sys.argv[1])
for port in range(start, start + 50):
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind(("127.0.0.1", port))
    except OSError:
        continue
    finally:
        sock.close()
    print(port)
    break
else:
    sys.exit(1)
PY
}

wait_health() {
  local port="$1"
  for _ in $(seq 1 120); do
    if curl -sf --max-time 2 "http://127.0.0.1:$port/api/health" >/dev/null 2>&1; then
      return 0
    fi
    if [[ -n "${SERVER_PID:-}" ]] && ! kill -0 "$SERVER_PID" 2>/dev/null; then
      return 1
    fi
    sleep 0.1
  done
  return 1
}

start_server() {
  local port="$1"
  shift
  SERVER_PORT="$port"
  # 注意：start.sh 内部 exec python3，因此 $! 就是最终服务进程（可直接 kill）
  "$ROOT/start.sh" --port "$port" "$@" >>"$TMP/server-$port.log" 2>&1 &
  SERVER_PID=$!
  if wait_health "$port"; then
    return 0
  fi
  say "---- 服务日志（端口 $port）----"
  tail -30 "$TMP/server-$port.log" 2>/dev/null | tee -a "$RESULT_LOG"
  return 1
}

# ---------------------------------------------------------------------
# 0. 预检
# ---------------------------------------------------------------------
say "===== 0. 预检 ====="
if [[ -x "$ROOT/start.sh" ]]; then
  pass "preflight.start_sh" "$ROOT/start.sh 可执行"
else
  fail "preflight.start_sh" "start.sh 缺失或不可执行"
  finalize
fi
if [[ -d /home/h/dsh-plugins/.scratch/genshin-mobile ]]; then
  pass "preflight.genshin_dir" "/home/h/dsh-plugins/.scratch/genshin-mobile 存在"
else
  fail "preflight.genshin_dir" "原神模块的 cwd 不存在"
fi
if [[ -s "$ROOT/README.md" ]]; then
  pass "preflight.readme" "README.md 已存在"
else
  fail "preflight.readme" "缺少 README.md"
fi

if "$ROOT/start.sh" --check >"$TMP/check.log" 2>&1; then
  pass "config.check" "$(grep '合计：' "$TMP/check.log" | tail -1)"
else
  fail "config.check" "$(tail -4 "$TMP/check.log" | tr '\n' ' ')"
fi

if ! command -v curl >/dev/null 2>&1; then
  fail "preflight.curl" "缺少 curl"
  finalize
fi

PORT="$(find_free_port "${SMOKE_PORT:-8978}" 2>/dev/null || true)"
if [[ -z "$PORT" ]]; then
  fail "preflight.port" "找不到空闲端口"
  finalize
fi
info "使用端口 $PORT（主阶段）"

say ""
say "===== 1. 启动服务 ====="
if start_server "$PORT"; then
  pass "server.start" "http://127.0.0.1:$PORT"
else
  fail "server.start" "健康检查未通过"
  finalize
fi

say ""
say "===== 2. curl 级验收（硬性要求 1/2） ====="
MODULES_JSON="$(curl -sS --max-time 5 "http://127.0.0.1:$PORT/api/modules")"
if python3 -c '
import json, sys
data = json.load(sys.stdin)
names = [m["name"] for m in data.get("modules", [])]
assert "原神触摸/串流" in names, names
assert len(next(m for m in data["modules"] if m["name"] == "原神触摸/串流")["commands"]) == 10
' <<<"$MODULES_JSON" 2>/dev/null; then
  pass "curl.modules.genshin" "含「原神触摸/串流」且 10 条命令"
else
  fail "curl.modules.genshin" "$(head -c 300 <<<"$MODULES_JSON")"
fi

HTTP_CODE="$(curl -sS --max-time 5 -D "$TMP/index.hdr" -o "$TMP/index.html" -w '%{http_code}' "http://127.0.0.1:$PORT/" 2>/dev/null || true)"
if [[ "$HTTP_CODE" == "200" ]] && grep -Fq 'CLI 命令管理器' "$TMP/index.html"; then
  pass "curl.page" "GET / → HTTP 200，含页面标题"
else
  fail "curl.page" "HTTP $HTTP_CODE"
fi
if grep -qi '^content-security-policy:' "$TMP/index.hdr"; then
  pass "curl.page.csp" "存在 Content-Security-Policy"
else
  fail "curl.page.csp" "缺少 CSP 头"
fi
if grep -qi '^x-frame-options: *DENY' "$TMP/index.hdr"; then
  pass "curl.page.frame_deny" "X-Frame-Options: DENY"
else
  fail "curl.page.frame_deny" "缺少或不是 DENY"
fi

JS_CODE="$(curl -sS --max-time 5 -o /dev/null -w '%{http_code}' "http://127.0.0.1:$PORT/static/app.js" 2>/dev/null || true)"
CSS_CODE="$(curl -sS --max-time 5 -o /dev/null -w '%{http_code}' "http://127.0.0.1:$PORT/static/style.css" 2>/dev/null || true)"
if [[ "$JS_CODE" == "200" && "$CSS_CODE" == "200" ]]; then
  pass "curl.static" "app.js / style.css 均 200"
else
  fail "curl.static" "app.js=$JS_CODE style.css=$CSS_CODE"
fi

HOST_CODE="$(curl -sS --max-time 5 -o /dev/null -w '%{http_code}' -H 'Host: evil.example' "http://127.0.0.1:$PORT/api/health" 2>/dev/null || true)"
if [[ "$HOST_CODE" == "403" ]]; then
  pass "curl.host_guard" "伪造 Host 被 403"
else
  fail "curl.host_guard" "伪造 Host 返回 HTTP $HOST_CODE"
fi

say ""
say "===== 3. 浏览器 DOM（硬性要求 2） ====="
if command -v chromium >/dev/null 2>&1; then
  if timeout 30s chromium --headless --no-sandbox --disable-gpu --disable-dev-shm-usage \
      --user-data-dir="$TMP/chrome-profile" --virtual-time-budget=5000 \
      --dump-dom "http://127.0.0.1:$PORT/" >"$TMP/dom.html" 2>"$TMP/chromium.err"; then
    CARDS="$(grep -o 'data-command-id="' "$TMP/dom.html" | wc -l | tr -d ' ')"
    if [[ "$CARDS" == "10" ]]; then
      pass "dom.command_cards" "默认模块渲染出 10 条命令卡片"
    else
      fail "dom.command_cards" "期望 10，实得 $CARDS"
    fi
    MODULE_ITEMS="$(grep -o 'class="module-item' "$TMP/dom.html" | wc -l | tr -d ' ')"
    if [[ "$MODULE_ITEMS" == "2" ]]; then
      pass "dom.module_items" "侧栏渲染出 2 个模块"
    else
      fail "dom.module_items" "期望 2，实得 $MODULE_ITEMS"
    fi
    dom_text() {
      local needle="$1" label="$2"
      if grep -Fq -- "$needle" "$TMP/dom.html"; then
        pass "dom.text.$label" "$needle"
      else
        fail "dom.text.$label" "页面 DOM 未包含：$needle"
      fi
    }
    dom_text './mobui-ui.sh status' 'ui_status_command'
    dom_text './mobui-gain.sh 35' 'set_gain_default'
    dom_text '查当前 UI 模式' 'ui_status_name'
    dom_text '原神触摸/串流' 'genshin_module'
    dom_text '演示模块' 'demo_module'
    dom_text '启动游戏+注入+切 UI' 'play_command'
  else
    fail "dom.render" "chromium 执行失败（见 $TMP/chromium.err）"
  fi
else
  skip "dom.render" "未找到 chromium，跳过浏览器渲染校验（API 级校验仍覆盖）"
fi

say ""
say "===== 4. API / SSE / 危险确认（硬性要求 3/4） ====="
python3 "$ROOT/test/api_check.py" --base-url "http://127.0.0.1:$PORT" --phase main \
  | tee -a "$RESULT_LOG"

say ""
say "===== 5. 热加载 + 配置健壮性（硬性要求 5） ====="
HOT_DIR="$TMP/hot-modules"
mkdir -p "$HOT_DIR"
cp "$ROOT"/modules/*.json "$HOT_DIR"/
HOT_PORT="$(find_free_port $((PORT + 1)) 2>/dev/null || true)"
if [[ -z "$HOT_PORT" ]]; then
  fail "hot.port" "找不到空闲端口"
else
  stop_server
  if start_server "$HOT_PORT" --modules-dir "$HOT_DIR"; then
    pass "server.start.hot" "http://127.0.0.1:$HOT_PORT（--modules-dir $HOT_DIR）"
    python3 "$ROOT/test/api_check.py" --base-url "http://127.0.0.1:$HOT_PORT" \
      --phase hot --modules-dir "$HOT_DIR" | tee -a "$RESULT_LOG"
  else
    fail "server.start.hot" "健康检查未通过"
  fi
fi
stop_server

say ""
say "===== 5b. 平台过滤（DOM，CLI_MANAGER_PLATFORM 预演） ====="
PLAT_DIR="$TMP/platform-modules"
mkdir -p "$PLAT_DIR"
cat > "$PLAT_DIR/win-fixture.json" <<'JSON'
{
  "id": "win-fixture",
  "name": "仅Windows示例",
  "order": 10,
  "platform": "windows",
  "cwd": "/tmp",
  "commands": [
    {"id": "hello", "name": "回声", "description": "echo hello-win", "risk": "low", "timeoutSec": 10, "argv": ["echo", "hello-win"]}
  ]
}
JSON

if command -v chromium >/dev/null 2>&1; then
  plat_dom() {  # plat_dom <port> <outfile> [env...]
    local port="$1" out="$2"; shift 2
    env "$@" "$ROOT/start.sh" --port "$port" --modules-dir "$PLAT_DIR" >"$TMP/server-$port.log" 2>&1 &
    local pid=$!
    local ok=0
    for _ in $(seq 1 50); do
      if curl -s --max-time 1 "http://127.0.0.1:$port/api/health" >/dev/null 2>&1; then ok=1; break; fi
      sleep 0.1
    done
    if [[ "$ok" != "1" ]]; then kill "$pid" 2>/dev/null; return 1; fi
    chromium --headless --no-sandbox --disable-gpu --disable-dev-shm-usage \
      --user-data-dir="$TMP/chrome-plat-$port" --virtual-time-budget=5000 \
      --dump-dom "http://127.0.0.1:$port/" >"$out" 2>"$TMP/chromium-$port.err" || { kill "$pid" 2>/dev/null; return 1; }
    kill "$pid" 2>/dev/null
    return 0
  }

  LINUX_PORT="$(find_free_port $((PORT + 20)) 2>/dev/null || true)"
  if [[ -n "$LINUX_PORT" ]] && plat_dom "$LINUX_PORT" "$TMP/dom-plat-linux.html"; then
    CARDS="$(grep -o 'data-command-id="' "$TMP/dom-plat-linux.html" | wc -l | tr -d ' ')"
    if [[ "$CARDS" == "0" ]]; then
      pass "platform.dom.linux_hidden" "Linux 平台下 windows 命令卡片数=0"
    else
      fail "platform.dom.linux_hidden" "期望 0，实得 $CARDS"
    fi
    if grep -Fq '已隐藏 1 条（其他平台）' "$TMP/dom-plat-linux.html"; then
      pass "platform.dom.hidden_note" "工具栏显示已隐藏计数"
    else
      fail "platform.dom.hidden_note" "未找到“已隐藏 1 条（其他平台）”"
    fi
  else
    fail "platform.dom.linux" "起服务或 chromium 失败"
  fi

  WIN_PORT="$(find_free_port $((PORT + 30)) 2>/dev/null || true)"
  if [[ -n "$WIN_PORT" ]] && plat_dom "$WIN_PORT" "$TMP/dom-plat-win.html" CLI_MANAGER_PLATFORM=windows; then
    CARDS="$(grep -o 'data-command-id="' "$TMP/dom-plat-win.html" | wc -l | tr -d ' ')"
    if [[ "$CARDS" == "1" ]]; then
      pass "platform.dom.windows_visible" "Windows 视角下卡片数=1"
    else
      fail "platform.dom.windows_visible" "期望 1，实得 $CARDS"
    fi
    if grep -Fq '仅 Windows' "$TMP/dom-plat-win.html"; then
      pass "platform.dom.chip" "卡片显示「仅 Windows」徽标"
    else
      fail "platform.dom.chip" "未找到「仅 Windows」徽标"
    fi
  else
    fail "platform.dom.windows" "起服务或 chromium 失败"
  fi
else
  skip "platform.dom" "没有 chromium，跳过 DOM 断言"
fi

say ""
say "===== 6. 收尾 ====="
info "主阶段端口: $PORT；热加载阶段端口: ${HOT_PORT:-—}"
info "启动命令: cd $ROOT && ./start.sh --port 8977"
info "模块配置: $ROOT/modules/*.json"

finalize
