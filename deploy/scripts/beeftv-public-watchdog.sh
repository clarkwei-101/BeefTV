#!/usr/bin/env bash
# ─── BeefTV Public Keepalive Watchdog ─────────────────────────────────────────
# 目标：Quick Tunnel + 后端 + 代理 + Web 常驻，死了自动拉起，URL 轮换自动更新。
# 不碰 Cloudflare 命名隧道（账号无域名），只守 Quick Tunnel。
#
# 用法：
#   ./deploy/scripts/beeftv-public-watchdog.sh                     # 前台跑（调试）
#   nohup ./deploy/scripts/beeftv-public-watchdog.sh >> /tmp/beeftv-watchdog.log 2>&1 &
#   launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.beeftv.public-watchdog.plist
#
# 日志约定：所有输出只写 stdout，由调用方重定向到日志文件。脚本内部不要再 tee 到
# 同一个文件——launchd 的 StandardOutPath 与 tee 会各写一遍，每条日志都会出现两行。
#
# 环境变量（不设置则用当前线上值，保持不断线）：
#   BEEFTV_BACKEND_DATA=/tmp/beeftv-e2e-data
#   BEEFTV_BACKEND_PORT=18080  BEEFTV_WEB_PORT=13001  BEEFTV_PROXY_PORT=8198
#   BEEFTV_PROXY_TOKEN=E2E_SMOKE_TOKEN_0123456789abcdef
#   BEEFTV_EXTRA_CORS_ORIGINS=https://beeftv-web.vercel.app
set -u
REPO_DIR="/Users/yahweh/Desktop/ai应用社/beeftv-web"
# 工作目录绝不能放在 ~/Desktop 下。
# launchd 起的进程受 TCC 限制，枚举 ~/Desktop 会**永久挂起**（不是报错）：
# Python 启动时会把 cwd 放进 sys.path，import 任意第三方模块都要扫描这个目录，
# 于是 `python -c "import fastapi"` 直接卡死，watchdog 只能打印"候选解释器不可用"。
# 所以这里退到 HOME，所有运行时操作都在 ~/.local/share/beeftv 下进行。
cd "$HOME" || exit 1

# 数据目录不能放在 /tmp：macOS 会定期清理 /tmp，之前 SQLite 库被清掉后
# 所有账户消失，接口全部 401，前端表现为"永远卡在加载页"。
# 现在固定放在 ~/.local/share/beeftv/data 下。
LEGACY_BACKEND_DATA="/tmp/beeftv-e2e-data"
BACKEND_DATA="${BEEFTV_BACKEND_DATA:-$HOME/.local/share/beeftv/data}"
BACKEND_PORT="${BEEFTV_BACKEND_PORT:-18080}"
WEB_PORT="${BEEFTV_WEB_PORT:-13001}"
PROXY_PORT="${BEEFTV_PROXY_PORT:-8198}"
BEEFTV_PROXY_TOKEN="${BEEFTV_PROXY_TOKEN:-E2E_SMOKE_TOKEN_0123456789abcdef}"
# Vercel 托管的前端是独立的源站，必须显式列入 CORS 白名单，否则后端会对
# 带 Origin 的请求返回 403 "不允许的跨域来源"。
BEEFTV_EXTRA_CORS_ORIGINS="${BEEFTV_EXTRA_CORS_ORIGINS:-https://beeftv-web.vercel.app,https://beeftv-web-clarkwei-101s-projects.vercel.app}"
URL_FILE="/tmp/beeftv_public_url"
TUNNEL_LOG="/tmp/beeftv-quick-tunnel-live.log"
WATCHDOG_LOG="/tmp/beeftv-watchdog.log"
BACKEND_LOG="/tmp/beeftv-backend-watchdog.log"
PROXY_LOG="/tmp/beeftv-proxy-watchdog.log"
SERVER_BIN="${BEEFTV_SERVER_BIN:-$HOME/.local/share/beeftv/beeftv-server}"
# 后端二进制不能放在 /tmp：macOS 会定期清理 /tmp，之前二进制被清掉后
# watchdog 想重建却读不到 ~/Desktop 下的仓库（launchd 进程受 TCC 限制），
# 于是后端再也没起来过 —— 整站 API 全 404，而前端只表现为"卡在加载页"。
# 现在二进制固定放在 ~/.local/share/beeftv 下，仓库读不到时继续用旧副本。
LEGACY_SERVER_BIN="/tmp/beeftv-server"
CLOUDFLARED="${CLOUDFLARED:-/opt/homebrew/bin/cloudflared}"
METRICS_ADDR="127.0.0.1:20244"
# launchd 起的进程受 TCC 限制，读不到 ~/Desktop 下的仓库文件（Python 报
# "can't open file: Operation not permitted"）。代理脚本因此放在 ~/.local 下
# 运行，仓库里的改动由 sync_proxy_script() 尽力同步；同步失败时沿用旧副本。
RUNTIME_DIR="$HOME/.local/share/beeftv"
PROXY_SCRIPT="$RUNTIME_DIR/beeftv_proxy.py"
# 官方插件包同理：后端启动会扫描 <工作目录>/plugin-packages，而工作目录在
# ~/Desktop 下时，launchd 起的进程读它会被 TCC 卡住（Go 的 open() 直接挂起，
# 不是报错），后端永远起不来。插件包因此复制到 ~/.local 下并用
# CANVAS_OFFICIAL_PLUGIN_DIR 显式指过去，工作目录也换到 RUNTIME_DIR。
PLUGIN_DIR="$RUNTIME_DIR/plugin-packages"
# Quick Tunnel 必须用干净的 HOME：~/.cloudflared/config.yml 里若存在命名隧道
# 配置，cloudflared 会按它的 ingress 路由，trycloudflare 主机名落到
# catch-all 404，公网全部 404，而且看日志里的 cred-file 才发现。
CLOUDFLARED_HOME="$RUNTIME_DIR/cloudflared-home"
# 单实例锁：手工 nohup 的副本会和 launchd 的权威实例抢端口。
LOCK_DIR="/tmp/beeftv-watchdog.lock"
# 日志上限：超过这个大小就裁剪，只保留尾部。
MAX_LOG_BYTES=$((20 * 1024 * 1024))

log() { echo "[$(date '+%m-%d %H:%M:%S')] $*" >&2; }

# ─── 受限仓库访问 ───
# launchd 起的进程受 macOS TCC 限制，读 ~/Desktop 下的仓库文件时不会返回
# 错误，而是**直接挂起**（Go 的 open() 曾经就这样卡死后端启动）。
# 而且挂起发生在内核的文件系统层，SIGALRM 也打断不了 —— 用 alarm 包一层
# 并不可靠。
#
# 因此这里的策略是：launchd 上下文里**根本不碰仓库**，一律使用
# ~/.local/share/beeftv 下的运行副本。仓库更新由开发环境手动同步
# （scripts/sync-beeftv-runtime.sh），watchdog 只负责保证副本可用。
#
# 需要在开发环境同步时，设置 BEEFTV_ALLOW_REPO_SYNC=1 打开这条路径。
REPO_READ_TIMEOUT="${REPO_READ_TIMEOUT:-8}"
ALLOW_REPO_SYNC="${BEEFTV_ALLOW_REPO_SYNC:-0}"

# repo_read <命令...>：执行一条会访问仓库的命令。
# 默认直接拒绝（TCC 下访问仓库会挂起，alarm 也救不回来）；
# 只有显式打开 BEEFTV_ALLOW_REPO_SYNC 时才真的执行，并套上超时保护。
repo_read() {
  if [ "$ALLOW_REPO_SYNC" != "1" ]; then
    return 1
  fi
  if command -v perl >/dev/null 2>&1; then
    perl -e 'alarm shift; exec @ARGV' "$REPO_READ_TIMEOUT" "$@" 2>/dev/null
  else
    "$@" 2>/dev/null
  fi
}

# repo_read_file <路径>：读一个仓库文件到标准输出，失败返回非 0。
repo_read_file() {
  repo_read cat "$1"
}

port_listen() { lsof -nP -iTCP:"$1" -sTCP:LISTEN 2>/dev/null | grep -q LISTEN; }

# truncate_log 只在日志超限时裁剪尾部，避免几十 MB 的日志把磁盘和排查都拖垮。
truncate_log() {
  local file="$1"
  [ -f "$file" ] || return 0
  local size
  size=$(wc -c < "$file" 2>/dev/null || echo 0)
  [ "$size" -gt "$MAX_LOG_BYTES" ] || return 0
  local tmp="$file.trim"
  tail -c $((MAX_LOG_BYTES / 2)) "$file" > "$tmp" 2>/dev/null || { rm -f "$tmp"; return 0; }
  mv "$tmp" "$file" 2>/dev/null || rm -f "$tmp"
  log "日志过大，已裁剪 $file（${size} 字节 → 保留尾部）"
}

# acquire_lock 保证只有一个 watchdog 实例在跑。
acquire_lock() {
  if mkdir "$LOCK_DIR" 2>/dev/null; then
    echo $$ > "$LOCK_DIR/pid"
    return 0
  fi
  local old
  old=$(cat "$LOCK_DIR/pid" 2>/dev/null || echo "")
  if [ -n "$old" ] && kill -0 "$old" 2>/dev/null; then
    return 1
  fi
  # 残留锁：持有者进程已经不在了，直接接管。
  # 这里不能用 pgrep -f 找"还有没有别的实例"：watchdog 自己 fork 出去的
  # 辅助子 shell 会继承脚本的 argv，被 pgrep 误判成第二个实例，
  # 结果是新实例死活起不来。锁目录 + 存活 PID 才是唯一权威依据：
  # mkdir 是原子操作，锁目录的 pid 文件必然属于当前唯一的持有者。
  rm -rf "$LOCK_DIR" 2>/dev/null || true
  mkdir "$LOCK_DIR" 2>/dev/null || return 1
  echo $$ > "$LOCK_DIR/pid"
  return 0
}

release_lock() {
  # 只释放自己持有的锁。
  # 之前是无条件 rm -rf：抢锁失败的实例退出时会把胜出实例的锁一起删掉，
  # 于是第三个实例又能拿到锁，线上就出现了两个 watchdog 互相抢端口、
  # 把对方刚拉起的后端当成 DOWN 再杀掉。
  local owner
  owner=$(cat "$LOCK_DIR/pid" 2>/dev/null || echo "")
  if [ "$owner" = "$$" ]; then
    rm -rf "$LOCK_DIR" 2>/dev/null || true
  fi
}

on_exit() {
  local code=$?
  if [ "$code" -ne 0 ]; then
    log "watchdog 异常退出 (code=$code)"
  fi
  release_lock
}
trap on_exit EXIT

# lock_owned_by_self 判断锁是否仍然属于自己。
# 锁被别的实例抢走（例如旧版本无条件 rm -rf 留下的残留目录）时，
# 继续跑就会两个 watchdog 互相把对方拉起的后端当成 DOWN 再杀掉。
lock_owned_by_self() {
  [ "$(cat "$LOCK_DIR/pid" 2>/dev/null || echo "")" = "$$" ]
}

# ─── 后端 ───
# backend_source_newer 判断源码是否比二进制新。launchd 上下文读不到 Desktop 时
# 返回 2，表示"无法判断"，调用方要按"未知"处理而不是当成陈旧。
backend_source_newer() {
  [ -x "$SERVER_BIN" ] || return 1
  # launchd 上下文访问 ~/Desktop 会挂起，默认不做这个检查。
  [ "$ALLOW_REPO_SYNC" = "1" ] || return 2
  local newer
  newer=$(repo_read find "$REPO_DIR/backend" -name '*.go' -newer "$SERVER_BIN" -print -quit)
  if [ -z "$newer" ]; then
    if ! repo_read stat "$REPO_DIR/backend" >/dev/null 2>&1; then
      return 2
    fi
    return 1
  fi
  return 0
}

ensure_backend() {
  if port_listen "$BACKEND_PORT"; then return 0; fi
  log "backend :$BACKEND_PORT DOWN → 重启中…"

  # 旧版本把二进制放在 /tmp，macOS 清理后就没了。这里做一次迁移，
  # 避免升级后第一次重启还要重新编译 48MB 的二进制。
  if [ ! -x "$SERVER_BIN" ] && [ -x "$LEGACY_SERVER_BIN" ]; then
    cp -f "$LEGACY_SERVER_BIN" "$SERVER_BIN" 2>/dev/null && chmod +x "$SERVER_BIN" \
      && log "后端二进制已从 $LEGACY_SERVER_BIN 迁移到 $SERVER_BIN"
  fi

  if [ ! -x "$SERVER_BIN" ]; then
    # launchd 上下文访问 ~/Desktop 下的仓库会挂起，不能在这里现场编译。
    # 二进制必须在开发环境构建好并放到 ~/.local/share/beeftv/beeftv-server。
    # 仅当显式打开仓库访问时才尝试现场构建。
    if [ "$ALLOW_REPO_SYNC" = "1" ] \
       && repo_read env -C "$REPO_DIR/backend" PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin" \
         go build -o "$SERVER_BIN" ./cmd/server >>"$BACKEND_LOG" 2>&1; then
      log "backend 已构建 → $SERVER_BIN"
    else
      log "backend 二进制缺失且无法现场构建；请在普通终端执行：cd $REPO_DIR/backend && go build -o $SERVER_BIN ./cmd/server"
      return 1
    fi
  fi
  # 旧版本把数据目录放在 /tmp，macOS 清理后账户全丢。这里做一次迁移：
  # 旧目录里还有库就搬过来，避免升级后所有用户被当成未注册。
  if [ ! -f "$BACKEND_DATA/open_ai_canvas.db" ] && [ -f "$LEGACY_BACKEND_DATA/open_ai_canvas.db" ]; then
    mkdir -p "$BACKEND_DATA" 2>/dev/null || true
    cp -R "$LEGACY_BACKEND_DATA/." "$BACKEND_DATA/" 2>/dev/null \
      && log "数据目录已从 $LEGACY_BACKEND_DATA 迁移到 $BACKEND_DATA"
  fi
  mkdir -p "$BACKEND_DATA"
  if ! sync_plugin_packages; then
    log "官方插件目录不可用（$PLUGIN_DIR）；请在普通终端执行：cp -R \"$REPO_DIR/plugin-packages\" \"$PLUGIN_DIR\""
    return 1
  fi
  # 注意：保持与线上进程一致的 env（注册开、token 一致），避免踢掉现有用户。
  # 工作目录必须是 RUNTIME_DIR：留在 ~/Desktop 下时后端启动会卡在插件目录扫描上。
  # 必须用 exec 收尾：`( ... & )` 会让 bash 再 fork 一个辅助子 shell，
  # 那个子 shell 继承脚本的 argv（"/bin/bash .../beeftv-public-watchdog.sh"），
  # 于是 pgrep -f beeftv-public-watchdog.sh 会把它当成第二个 watchdog 实例，
  # 单实例判断随之失灵。exec 之后该进程直接变成后端，argv 里不再有脚本名。
  (
    cd "$RUNTIME_DIR" || exit 1
    exec env \
      CANVAS_BACKEND_DATA_DIR="$BACKEND_DATA" \
      CANVAS_DATABASE_DRIVER=sqlite \
      CANVAS_REGISTRATION_ENABLED=true \
      CANVAS_ALLOW_PRIVATE_UPSTREAMS=false \
      CANVAS_ALLOWED_PRIVATE_UPSTREAM_HOSTS="127.0.0.1,localhost,192.168.1.193" \
      CANVAS_CORS_ORIGINS="http://127.0.0.1:3000,$(cat "$URL_FILE" 2>/dev/null),${BEEFTV_EXTRA_CORS_ORIGINS:-}" \
      CANVAS_BACKEND_ADDR=":$BACKEND_PORT" \
      CANVAS_OFFICIAL_PLUGIN_DIR="$PLUGIN_DIR" \
      BEEFTV_PROXY_TOKEN="$BEEFTV_PROXY_TOKEN" \
      nohup "$SERVER_BIN" >>"$BACKEND_LOG" 2>&1 </dev/null
  ) &
  # launchd 上下文里后端冷启动实测约 35 秒（要扫数据目录、迁移旧数据、打开 SQLite WAL），
  # 30 秒的窗口会误报"重启失败"。窗口放宽到 60 秒，避免日志里出现假的失败。
  for _ in $(seq 1 30); do
    sleep 2
    port_listen "$BACKEND_PORT" && { log "backend 已恢复 :$BACKEND_PORT"; return 0; }
  done
  log "backend 仍未就绪（60 秒内），日志尾部："
  tail -5 "$BACKEND_LOG" 2>/dev/null
  return 1
}
# sync_plugin_packages 把官方插件包从仓库复制到 ~/.local 下的运行目录。
# launchd 进程读不到 ~/Desktop 时复制会失败；此时沿用已有副本，
# 只有连已有副本都没有才算失败（后端启动必须有插件目录）。
sync_plugin_packages() {
  mkdir -p "$PLUGIN_DIR" 2>/dev/null || return 1
  # 运行副本已存在就直接用：launchd 上下文访问 ~/Desktop 下的仓库会挂起。
  if ls "$PLUGIN_DIR"/*.beeftv-plugin >/dev/null 2>&1; then
    if [ "$ALLOW_REPO_SYNC" = "1" ]; then
      repo_read cp -R "$REPO_DIR/plugin-packages/." "$PLUGIN_DIR/" 2>/dev/null || true
    fi
    return 0
  fi
  # 运行副本缺失时才需要从仓库复制。
  repo_read cp -R "$REPO_DIR/plugin-packages/." "$PLUGIN_DIR/" 2>/dev/null || return 1
  log "官方插件包已同步到 $PLUGIN_DIR"
  ls "$PLUGIN_DIR"/*.beeftv-plugin >/dev/null 2>&1
}

# ─── 代理 ───
# 把仓库里的代理脚本同步到 ~/.local 下运行。launchd 进程读不到 ~/Desktop，
# 所以直接跑仓库路径会报 "Operation not permitted"。
sync_proxy_script() {
  mkdir -p "$RUNTIME_DIR" 2>/dev/null || return 1
  # 运行副本已存在就直接用它：launchd 上下文访问 ~/Desktop 下的仓库会挂起，
  # 不能让"同步"这一步阻塞整个 watchdog。
  if [ -f "$PROXY_SCRIPT" ]; then
    if [ "$ALLOW_REPO_SYNC" = "1" ]; then
      # 只在内容不同时复制，避免每次重启都改 mtime。
      if ! repo_read cmp -s "$REPO_DIR/beeftv_proxy.py" "$PROXY_SCRIPT"; then
        repo_read cp -f "$REPO_DIR/beeftv_proxy.py" "$PROXY_SCRIPT" 2>/dev/null \
          || { log "代理脚本同步失败（沿用旧副本）"; return 0; }
        log "代理脚本已同步到 $PROXY_SCRIPT"
      fi
    fi
    return 0
  fi
  # 运行副本缺失时才需要从仓库复制。
  repo_read cp -f "$REPO_DIR/beeftv_proxy.py" "$PROXY_SCRIPT" 2>/dev/null || return 1
  return 0
}

# pick_proxy_python 选一个真的能 import fastapi 的解释器。
#
# 候选顺序很重要：仓库里的 .venv 位于 ~/Desktop 下，launchd 起的进程读它会被
# TCC 拒绝，报 PermissionError: .venv/pyvenv.cfg；家目录下的 uv python 没有这个
# 问题，而且实测带 fastapi/uvicorn。
pick_proxy_python() {
  local candidate
  # 注意：解释器自检必须在**安全目录**下执行。
  # Python 启动时会把 cwd 放进 sys.path，import 任意第三方包都要扫描该目录；
  # launchd 进程在 ~/Desktop 下时这个扫描会永久挂起（macOS TCC），
  # 表现为"候选解释器不可用"，实际是卡死。
  # 这里用子 shell 把 cwd 钉在 RUNTIME_DIR，避免污染主流程。
  local py
  py=$(
    cd "$RUNTIME_DIR" 2>/dev/null || exit 1
    for candidate in "${BEEFTV_PROXY_PYTHON:-}" "$HOME/.local/bin/python3" "/usr/local/bin/python3" "/opt/homebrew/bin/python3"; do
      [ -n "$candidate" ] || continue
      [ -x "$candidate" ] || continue
      if "$candidate" -c "import fastapi, uvicorn" >/dev/null 2>&1; then
        echo "$candidate"
        exit 0
      fi
      echo "  候选解释器不可用：$candidate" >&2
    done
    # 仓库里的 .venv 位于 ~/Desktop 下，launchd 进程执行它会挂起 ——
    # 只有显式打开仓库访问时才试它，而且必须带超时保护。
    if [ "$ALLOW_REPO_SYNC" = "1" ] && repo_read test -x "$REPO_DIR/.venv/bin/python3"; then
      if repo_read "$REPO_DIR/.venv/bin/python3" -c "import fastapi, uvicorn" >/dev/null 2>&1; then
        echo "$REPO_DIR/.venv/bin/python3"
        exit 0
      fi
    fi
    local fallback
    fallback=$(command -v python3 2>/dev/null || echo "")
    if [ -n "$fallback" ] && "$fallback" -c "import fastapi, uvicorn" >/dev/null 2>&1; then
      echo "$fallback"
      exit 0
    fi
    echo "  兜底解释器也不可用：${fallback:-无}" >&2
    exit 1
  ) || true
  [ -n "$py" ] || return 1
  echo "$py"
  return 0
}

ensure_proxy() {
  if port_listen "$PROXY_PORT"; then return 0; fi
  log "proxy :$PROXY_PORT DOWN → 重启中…"
  sync_proxy_script || true
  if [ ! -f "$PROXY_SCRIPT" ]; then
    log "代理脚本不可用：$PROXY_SCRIPT 缺失且同步失败"
    return 1
  fi
  local py
  py="$(pick_proxy_python || true)"
  if [ -z "$py" ]; then
    log "找不到带 fastapi 的 Python 解释器；请安装后重试（候选：$HOME/.local/bin/python3）"
    return 1
  fi
  cd "$RUNTIME_DIR" || return 1
  # 解释器自检必须在一个安全目录里跑：Python 会把 cwd 放进 sys.path，
  # cwd 在 ~/Desktop 下时 launchd 进程 import 会挂死。
  # launchd 给的 PATH 只有系统目录，Homebrew 的 ffmpeg/ffprobe 不在其中；
  # 代理内部也会自己兜底，但这里补上 PATH 能让所有外部工具都能被找到。
  PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin" \
  BEEFTV_PROXY_LISTEN=0.0.0.0 \
  BEEFTV_PROXY_PORT="$PROXY_PORT" \
  BEEFTV_PROXY_TOKEN="$BEEFTV_PROXY_TOKEN" \
    nohup "$py" "$PROXY_SCRIPT" >>"$PROXY_LOG" 2>&1 </dev/null &
  # 不回到仓库目录：留在 RUNTIME_DIR，避免后续步骤把 ~/Desktop 传给出生的进程。
  for _ in $(seq 1 15); do
    sleep 2
    curl -sf --max-time 3 "http://127.0.0.1:$PROXY_PORT/health" >/dev/null 2>&1 \
      && { log "proxy 已恢复 :$PROXY_PORT (python=$py)"; return 0; }
  done
  log "proxy 重启失败 (python=$py)，日志尾部："
  tail -5 "$PROXY_LOG" 2>/dev/null
  return 1
}

# ─── Web ───
# launchd 上下文读不到 ~/Desktop 下的仓库，所以 Web 层不能靠仓库里的 vite。
# 做法是：构建产物同步到 ~/.local/share/beeftv/web，用独立 nginx 托管静态资源并把
# /api 反代到本机后端；仓库读不到时沿用上一份已构建的副本。
NGINX_BIN="${NGINX_BIN:-/opt/homebrew/bin/nginx}"
NGINX_DIR="$RUNTIME_DIR/nginx"
NGINX_CONF="$NGINX_DIR/nginx.conf"
WEB_ROOT="$RUNTIME_DIR/web"
NGINX_LOG_DIR="$NGINX_DIR/logs"
NGINX_RUN_DIR="$NGINX_DIR/tmp"

# nginx 的 mime.types 跟着 Homebrew 前缀走，找不到就退回常见路径。
resolve_mime_types() {
  local prefix
  prefix=$(dirname "$(dirname "$NGINX_BIN")")
  for candidate in "$prefix/etc/nginx/mime.types" "/opt/homebrew/etc/nginx/mime.types" "/usr/local/etc/nginx/mime.types"; do
    [ -f "$candidate" ] && { echo "$candidate"; return 0; }
  done
  return 1
}

# stage_web_build 把 web/dist 同步到运行时目录。先同步到临时目录再整体替换，
# 避免只复制了一半就换掉线上可用的构建产物。
stage_web_build() {
  mkdir -p "$RUNTIME_DIR" "$NGINX_DIR" "$NGINX_LOG_DIR" "$NGINX_RUN_DIR" 2>/dev/null || return 1
  # launchd 上下文不能访问 ~/Desktop 下的仓库（TCC 会挂起访问），
  # 所以默认只用已经构建好的运行副本；仓库同步由开发环境手动执行。
  local staging="$RUNTIME_DIR/web.new"
  rm -rf "$staging" 2>/dev/null || true
  mkdir -p "$staging" 2>/dev/null || return 1
  if [ "$ALLOW_REPO_SYNC" = "1" ] && repo_read test -d "$REPO_DIR/web/dist"; then
    if command -v rsync >/dev/null 2>&1; then
      repo_read rsync -a "$REPO_DIR/web/dist/" "$staging/" >/dev/null 2>&1 || true
    else
      repo_read cp -R "$REPO_DIR/web/dist/." "$staging/" >/dev/null 2>&1 || true
    fi
  fi
  if [ -f "$staging/index.html" ]; then
    rm -rf "$WEB_ROOT" 2>/dev/null || true
    mv "$staging" "$WEB_ROOT" 2>/dev/null || true
  else
    rm -rf "$staging" 2>/dev/null || true
  fi
  # 仓库读不到（launchd TCC）时沿用上一份副本，同样算成功。
  [ -f "$WEB_ROOT/index.html" ]
}

# render_nginx_conf 用当前端口和数据目录渲染 nginx 配置。
# 和 stage_web_build 一样：模板读不到时沿用已渲染的配置，否则 nginx 再也起不来。
render_nginx_conf() {
  local src="$REPO_DIR/deploy/scripts/nginx-public.conf"
  mkdir -p "$NGINX_DIR" "$NGINX_LOG_DIR" "$NGINX_RUN_DIR" 2>/dev/null || return 1
  # 模板在 ~/Desktop 下，launchd 上下文读它会挂起 —— 默认沿用已渲染的配置。
  if [ "$ALLOW_REPO_SYNC" = "1" ] && repo_read test -f "$src"; then
    local mime
    mime=$(resolve_mime_types || true)
    if [ -n "$mime" ]; then
      repo_read sed -e "s|__LISTEN_PORT__|$WEB_PORT|g" \
          -e "s|__WEB_ROOT__|$WEB_ROOT|g" \
          -e "s|__BACKEND_HOST__|127.0.0.1|g" \
          -e "s|__BACKEND_PORT__|$BACKEND_PORT|g" \
          -e "s|__LOG_DIR__|$NGINX_LOG_DIR|g" \
          -e "s|__RUN_DIR__|$NGINX_RUN_DIR|g" \
          -e "s|__MIME_TYPES__|$mime|g" \
          "$src" > "$NGINX_CONF.new" 2>/dev/null && [ -s "$NGINX_CONF.new" ] \
          && mv "$NGINX_CONF.new" "$NGINX_CONF" 2>/dev/null
      rm -f "$NGINX_CONF.new" 2>/dev/null || true
    fi
  fi
  [ -s "$NGINX_CONF" ]
}

ensure_web() {
  if port_listen "$WEB_PORT"; then return 0; fi
  log "web :$WEB_PORT DOWN → 重启中…"
  # 1) 首选 nginx 托管静态构建产物：不需要在 launchd 上下文里读仓库。
  if stage_web_build && render_nginx_conf && [ -x "$NGINX_BIN" ]; then
    if "$NGINX_BIN" -c "$NGINX_CONF" -p "$NGINX_DIR" >/dev/null 2>&1; then
      for _ in $(seq 1 10); do
        sleep 1
        port_listen "$WEB_PORT" && { log "web 已恢复 :$WEB_PORT (nginx)"; return 0; }
      done
    fi
    log "nginx 启动失败，配置与错误日志："
    tail -5 "$NGINX_LOG_DIR/error.log" 2>/dev/null
    "$NGINX_BIN" -c "$NGINX_CONF" -p "$NGINX_DIR" -t 2>&1 | tail -3
  fi
  # 2) 回退到仓库里的 vite 开发服务器（需要有 Desktop 访问权限的终端环境）。
  if [ "$ALLOW_REPO_SYNC" = "1" ] && repo_read test -d "$REPO_DIR/web/node_modules"; then
    # 同样用 exec 收尾，避免辅助子 shell 继承脚本 argv。
    (
      cd "$REPO_DIR/web" || exit 1
      exec env VITE_API_PROXY_TARGET="http://127.0.0.1:$BACKEND_PORT" \
        nohup node ./node_modules/.bin/vite --host 0.0.0.0 --port "$WEB_PORT" \
        >>"/tmp/beeftv-web-watchdog.log" 2>&1
    ) &
    for _ in $(seq 1 15); do
      sleep 3
      port_listen "$WEB_PORT" && { log "web 已恢复 :$WEB_PORT (vite)"; return 0; }
    done
  fi
  log "web 重启失败"
  return 1
}

# ─── Quick Tunnel ───
current_url() { cat "$URL_FILE" 2>/dev/null | tr -d ' \n'; }

tunnel_alive() {
  pgrep -f "cloudflared tunnel --url http://127.0.0.1:$WEB_PORT" >/dev/null 2>&1 || \
  pgrep -f "cloudflared.*127.0.0.1:$WEB_PORT" >/dev/null 2>&1
}

resolve_cloudflared() {
  if [ -x "$CLOUDFLARED" ]; then
    echo "$CLOUDFLARED"
    return 0
  fi
  local found
  found=$(command -v cloudflared 2>/dev/null || echo "")
  if [ -n "$found" ] && [ -x "$found" ]; then
    echo "$found"
    return 0
  fi
  return 1
}

ensure_tunnel() {
  local url old_url
  url="$(current_url)"
  old_url="$url"
  # 进程活着 + 公网能访问 → 健康
  if tunnel_alive && [ -n "$url" ]; then
    if curl -s --max-time 10 -o /dev/null -w "%{http_code}" "$url/" 2>/dev/null | grep -q "200\|302\|301"; then
      return 0
    fi
    log "tunnel 进程在但公网不通 ($url) → 重拉…"
    pkill -f "cloudflared.*127.0.0.1:$WEB_PORT" 2>/dev/null || true
    sleep 2
  elif tunnel_alive; then
    return 0
  else
    log "tunnel 进程 DOWN → 重拉…"
  fi
  local cloudflared
  cloudflared="$(resolve_cloudflared || true)"
  if [ -z "$cloudflared" ]; then
    log "找不到 cloudflared，可执行文件缺失"
    return 1
  fi
  # 拉起新隧道并解析 URL。HOME 指向干净目录，避免加载 ~/.cloudflared/config.yml
  # 里的命名隧道 ingress（那会让 trycloudflare 主机名全部 404）。
  : > "$TUNNEL_LOG"
  mkdir -p "$CLOUDFLARED_HOME" 2>/dev/null || true
  HOME="$CLOUDFLARED_HOME" nohup "$cloudflared" tunnel --url "http://127.0.0.1:$WEB_PORT" --no-autoupdate \
    --metrics "$METRICS_ADDR" >"$TUNNEL_LOG" 2>&1 &
  for _ in $(seq 1 30); do
    sleep 2
    url="$(grep -oE 'https://[a-z0-9-]+\.trycloudflare\.com' "$TUNNEL_LOG" | head -1)"
    if [ -n "$url" ]; then
      if grep -q "cred-file:" "$TUNNEL_LOG"; then
        log "cloudflared 仍加载了命名隧道凭证，隧道不可用"
        tail -3 "$TUNNEL_LOG" 2>/dev/null
        return 1
      fi
      echo -n "$url" > "$URL_FILE"
      log "tunnel 已恢复 → $url"
      if [ -n "$old_url" ] && [ "$url" != "$old_url" ]; then
        # 隧道地址轮换：后端 CORS 白名单里还是旧地址，重启后端载入新地址。
        log "公网地址变化 ($old_url → $url)，重启后端更新 CORS…"
        pkill -f "$SERVER_BIN" 2>/dev/null || true
        sleep 2
        ensure_backend || true
      fi
      return 0
    fi
    tunnel_alive || { log "cloudflared 启动即退出，日志："; tail -5 "$TUNNEL_LOG" 2>/dev/null; return 1; }
  done
  log "tunnel 重拉超时"
  return 1
}

if ! acquire_lock; then
  log "已有 watchdog 实例在运行，本次退出"
  exit 0
fi

log "watchdog 启动 (backend:$BACKEND_PORT web:$WEB_PORT proxy:$PROXY_PORT)"
log "当前公网: $(current_url)"
# 防止 Mac 休眠导致断线（用户要求不断线）
if [ ! -f /tmp/beeftv-caffeinate.pid ] || ! kill -0 "$(cat /tmp/beeftv-caffeinate.pid 2>/dev/null)" 2>/dev/null; then
  nohup caffeinate -dims -w $$ >/dev/null 2>&1 &
  echo $! > /tmp/beeftv-caffeinate.pid
  log "caffeinate 已启动（防休眠断线）"
fi

stale_warned=0
while true; do
  # 自检锁归属：锁被别的实例抢走时立刻退出，避免两个 watchdog 抢端口。
  if ! lock_owned_by_self; then
    log "锁已被其他实例接管，本次退出"
    exit 0
  fi
  truncate_log "$BACKEND_LOG"
  truncate_log "$PROXY_LOG"
  truncate_log "$WATCHDOG_LOG"
  # 后端二进制比源码旧时提示一次：launchd 上下文无法自己重建。
  backend_source_newer
  case $? in
    0)
      if [ "$stale_warned" -eq 0 ]; then
        log "后端二进制已过期（源码比 $SERVER_BIN 新），当前仍在跑旧版本；请在普通终端重建：cd $REPO_DIR/backend && go build -o $SERVER_BIN ./cmd/server && pkill -f $SERVER_BIN"
        stale_warned=1
      fi
      ;;
  esac
  ensure_proxy || true
  ensure_backend || true
  ensure_web || true
  if ensure_tunnel; then
    sleep 20
  else
    # Quick Tunnel 有速率限制（429/1015）。失败后立刻重试只会让限流更久，
    # 所以退避一分钟再试。
    sleep 60
  fi
done