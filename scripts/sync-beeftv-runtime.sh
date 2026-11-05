#!/usr/bin/env bash
# ─── BeefTV 运行时同步 ────────────────────────────────────────────────────────
# 把仓库里的运行时文件同步到 ~/.local/share/beeftv/ 下，供 launchd 里的
# watchdog 使用。
#
# 为什么需要这一步：launchd 起的进程受 macOS TCC 限制，读 ~/Desktop 下的文件
# 不是报错，而是**永久挂起**（内核层的 open/stat）。所以 watchdog 在
# launchd 上下文里完全不碰仓库，一律使用 ~/.local/share/beeftv 下的副本。
# 仓库改了之后，需要在普通终端（带桌面访问权限）跑一次本脚本。
#
# 用法：bash scripts/sync-beeftv-runtime.sh
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RUNTIME_DIR="$HOME/.local/share/beeftv"

log() { echo "[sync] $*"; }

mkdir -p "$RUNTIME_DIR"

# 1) 代理脚本
if [ -f "$REPO_DIR/beeftv_proxy.py" ]; then
  cp -f "$REPO_DIR/beeftv_proxy.py" "$RUNTIME_DIR/beeftv_proxy.py"
  log "代理脚本 → $RUNTIME_DIR/beeftv_proxy.py"
fi

# 2) 官方插件包（后端启动会扫描这个目录）
if [ -d "$REPO_DIR/plugin-packages" ]; then
  mkdir -p "$RUNTIME_DIR/plugin-packages"
  cp -R "$REPO_DIR/plugin-packages/." "$RUNTIME_DIR/plugin-packages/"
  log "插件包 → $RUNTIME_DIR/plugin-packages"
fi

# 3) 前端构建产物（nginx 托管）
if [ -d "$REPO_DIR/web/dist" ]; then
  rm -rf "$RUNTIME_DIR/web.new" "$RUNTIME_DIR/web"
  mkdir -p "$RUNTIME_DIR/web.new"
  cp -R "$REPO_DIR/web/dist/." "$RUNTIME_DIR/web.new/"
  mv "$RUNTIME_DIR/web.new" "$RUNTIME_DIR/web"
  log "前端构建 → $RUNTIME_DIR/web"
else
  log "跳过前端：$REPO_DIR/web/dist 不存在（先 cd web && bun run build）"
fi

# 4) watchdog 脚本本身（launchd 直接执行这份）
if [ -f "$REPO_DIR/deploy/scripts/beeftv-public-watchdog.sh" ]; then
  cp -f "$REPO_DIR/deploy/scripts/beeftv-public-watchdog.sh" "$HOME/.local/bin/beeftv-public-watchdog.sh"
  chmod +x "$HOME/.local/bin/beeftv-public-watchdog.sh"
  log "watchdog → $HOME/.local/bin/beeftv-public-watchdog.sh"
fi

# 5) 后端二进制（仓库源码变更时重新构建）
if [ -d "$REPO_DIR/backend" ]; then
  log "构建后端二进制…"
  ( cd "$REPO_DIR/backend" && go build -o "$RUNTIME_DIR/beeftv-server" ./cmd/server )
  log "后端 → $RUNTIME_DIR/beeftv-server"
fi

# 6) 重启 watchdog 让新副本生效
if launchctl list 2>/dev/null | grep -q "com.beeftv.public-watchdog"; then
  launchctl kickstart -k "gui/$(id -u)/com.beeftv.public-watchdog"
  log "watchdog 已重启"
fi

log "完成"
