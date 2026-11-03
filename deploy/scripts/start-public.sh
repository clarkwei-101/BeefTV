#!/usr/bin/env bash
# ─── BeefTV Public Deployment (entry point) ───────────────────────────────────
# 公网形态只有一个实现：deploy/scripts/beeftv-public-watchdog.sh（常驻）。
# 本脚本是它的入口，负责：
#   1. 把 watchdog 同步到 ~/.local/bin/（launchd 常驻脚本不能放 ~/Desktop，TCC 会拦）
#   2. 发布前端构建产物到 ~/.local/share/beeftv/web（watchdog 自己读不到仓库）
#   3. 启停 launchd 常驻实例，并打印当前公网地址
#
# 端口约定（与 watchdog 一致，不要和本地形态的 start.sh 混用）：
#   后端 18080 / Web 13001 / 代理 8198，只有 Web 通过 Quick Tunnel 对外。
#
# 用法：
#   ./deploy/scripts/start-public.sh start    # 发布前端 + 拉起常驻 watchdog
#   ./deploy/scripts/start-public.sh stop     # 停掉 watchdog 与三个服务
#   ./deploy/scripts/start-public.sh status   # 查看端口与公网地址
#   ./deploy/scripts/start-public.sh publish  # 只发布前端构建产物
#   ./deploy/scripts/start-public.sh token    # 生成一个新的代理 token

set -uo pipefail

REPO_DIR="$(cd "$(dirname "$0")/../.." && pwd)"
RUNTIME_DIR="${BEEFTV_PUBLIC_RUNTIME_DIR:-$HOME/.local/share/beeftv}"
WATCHDOG_REPO="$REPO_DIR/deploy/scripts/beeftv-public-watchdog.sh"
WATCHDOG_BIN="$HOME/.local/bin/beeftv-public-watchdog.sh"
LAUNCH_AGENT="com.beeftv.public-watchdog"
PLIST="$HOME/Library/LaunchAgents/$LAUNCH_AGENT.plist"
URL_FILE="/tmp/beeftv_public_url"
BACKEND_PORT="${BEEFTV_BACKEND_PORT:-18080}"
WEB_PORT="${BEEFTV_WEB_PORT:-13001}"
PROXY_PORT="${BEEFTV_PROXY_PORT:-8198}"

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; CYAN='\033[0;36m'; NC='\033[0m'
log_info()  { echo -e "${GREEN}[INFO]${NC}  $1"; }
log_warn()  { echo -e "${YELLOW}[WARN]${NC}  $1"; }
log_error() { echo -e "${RED}[ERROR]${NC} $1"; }

port_listen() { lsof -nP -iTCP:"$1" -sTCP:LISTEN 2>/dev/null | grep -q LISTEN; }

sync_watchdog() {
    [ -f "$WATCHDOG_REPO" ] || { log_error "找不到 $WATCHDOG_REPO"; return 1; }
    mkdir -p "$(dirname "$WATCHDOG_BIN")"
    if ! diff -q "$WATCHDOG_REPO" "$WATCHDOG_BIN" >/dev/null 2>&1; then
        # 先写临时文件再 mv：直接覆盖正在运行的脚本会让 bash 的读取偏移错位，
        # 下一次循环可能执行到半截内容。mv 换 inode，运行中的实例不受影响。
        cp "$WATCHDOG_REPO" "$WATCHDOG_BIN.tmp"
        chmod +x "$WATCHDOG_BIN.tmp"
        mv -f "$WATCHDOG_BIN.tmp" "$WATCHDOG_BIN"
        xattr -d com.apple.quarantine "$WATCHDOG_BIN" 2>/dev/null || true
        log_info "watchdog 已同步到 $WATCHDOG_BIN（需 kickstart 才会用上新内容）"
    fi
}

publish_web() {
    if [ ! -x "$REPO_DIR/deploy/scripts/publish-public-web.sh" ]; then
        log_error "缺少 deploy/scripts/publish-public-web.sh"
        return 1
    fi
    "$REPO_DIR/deploy/scripts/publish-public-web.sh" || return 1
}

ensure_launch_agent() {
    mkdir -p "$(dirname "$PLIST")"
    if [ ! -f "$PLIST" ]; then
        cat >"$PLIST" <<PLIST_EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LAUNCH_AGENT</string>
  <key>ProgramArguments</key><array><string>/bin/bash</string><string>$WATCHDOG_BIN</string></array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ThrottleInterval</key><integer>30</integer>
  <key>ProcessType</key><string>Background</string>
  <key>StandardOutPath</key><string>/tmp/beeftv-watchdog.log</string>
  <key>StandardErrorPath</key><string>/tmp/beeftv-watchdog.log</string>
</dict>
</plist>
PLIST_EOF
        log_info "已生成 launchd 配置 $PLIST"
    fi
    launchctl bootout "gui/$(id -u)/$LAUNCH_AGENT" 2>/dev/null || true
    launchctl bootstrap "gui/$(id -u)" "$PLIST" 2>/dev/null || true
    launchctl kickstart -k "gui/$(id -u)/$LAUNCH_AGENT" 2>/dev/null || true
}

case "${1:-start}" in
    start)
        sync_watchdog || exit 1
        publish_web || log_warn "前端发布失败，线上继续用旧包"
        ensure_launch_agent
        log_info "watchdog 已交给 launchd 常驻"
        for _ in $(seq 1 30); do
            sleep 2
            if port_listen "$WEB_PORT" && port_listen "$BACKEND_PORT"; then
                break
            fi
        done
        log_info "后端 :$BACKEND_PORT / Web :$WEB_PORT / 代理 :$PROXY_PORT"
        if [ -s "$URL_FILE" ]; then
            log_info "公网地址：$(cat "$URL_FILE")"
        else
            log_warn "公网地址还没写进 $URL_FILE，看 /tmp/beeftv-watchdog.log"
        fi
        ;;
    stop)
        launchctl bootout "gui/$(id -u)/$LAUNCH_AGENT" 2>/dev/null || true
        pkill -f "$WATCHDOG_BIN" 2>/dev/null || true
        pkill -f "/tmp/beeftv-server" 2>/dev/null || true
        pkill -f "beeftv_proxy.py" 2>/dev/null || true
        pkill -f "cloudflared.*127.0.0.1:$WEB_PORT" 2>/dev/null || true
        log_info "已停止 watchdog 与三个服务"
        ;;
    status)
        for entry in "后端:$BACKEND_PORT" "Web:$WEB_PORT" "代理:$PROXY_PORT"; do
            name="${entry%%:*}"; port="${entry##*:}"
            if port_listen "$port"; then log_info "$name :$port UP"; else log_warn "$name :$port DOWN"; fi
        done
        if [ -s "$URL_FILE" ]; then
            echo -e "${CYAN}公网地址：$(cat "$URL_FILE")${NC}"
        else
            log_warn "没有公网地址记录"
        fi
        ;;
    publish)
        publish_web
        ;;
    token)
        openssl rand -hex 32 2>/dev/null || python3 -c "import secrets; print(secrets.token_hex(32))"
        ;;
    *)
        echo "用法：$0 {start|stop|status|publish|token}"
        exit 1
        ;;
esac
