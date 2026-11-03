#!/usr/bin/env bash
# 把 web/dist 发布到公网运行目录并 reload nginx。
#
# 为什么需要单独一步：公网常驻的 watchdog 是 launchd 拉起的，macOS TCC 会让它
# 读不到 ~/Desktop 下的仓库，于是 watchdog 里的 stage_web_build 会静默沿用旧
# 副本（日志里看不到报错，但线上还是旧包）。所以构建产物必须在普通终端里
# 发布到 ~/.local/share/beeftv/web —— 那个目录 watchdog 读得到。
#
# 用法：./deploy/scripts/publish-public-web.sh
set -euo pipefail

cd "$(dirname "$0")/../.."
REPO_DIR="$(pwd)"

RUNTIME_DIR="${BEEFTV_PUBLIC_RUNTIME_DIR:-$HOME/.local/share/beeftv}"
WEB_ROOT="$RUNTIME_DIR/web"
NGINX_DIR="$RUNTIME_DIR/nginx"
NGINX_CONF="$NGINX_DIR/nginx.conf"
NGINX_BIN="${BEEFTV_NGINX_BIN:-$(command -v nginx || echo /opt/homebrew/bin/nginx)}"

log() { echo "[publish-web] $*"; }

if [ ! -d "$REPO_DIR/web" ]; then
  log "仓库不存在：$REPO_DIR/web"
  exit 1
fi

log "构建前端…"
(cd "$REPO_DIR/web" && bun run build)

if [ ! -f "$REPO_DIR/web/dist/index.html" ]; then
  log "构建产物缺失：web/dist/index.html"
  exit 1
fi

mkdir -p "$WEB_ROOT"
log "发布到 $WEB_ROOT"
rsync -a --delete "$REPO_DIR/web/dist/" "$WEB_ROOT/"

if [ -x "$NGINX_BIN" ] && [ -f "$NGINX_CONF" ]; then
  log "reload nginx"
  "$NGINX_BIN" -c "$NGINX_CONF" -p "$NGINX_DIR" -s reload
else
  log "找不到 nginx 或配置，跳过 reload（下次 watchdog 重启 web 时会加载新包）"
fi

log "完成：$WEB_ROOT/index.html"
