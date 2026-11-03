#!/bin/bash
# BeeFTV Web Edition - Start Script
# Connects MiniMax H3 + Wan 2.1 on DGX Spark through BeefTV web interface

set -e
cd "$(dirname "$0")"

BACKEND_DATA="${BEEFTV_DATA:-./backend/data}"
BACKEND_PORT="${BEEFTV_BACKEND_PORT:-8080}"
WEB_PORT="${BEEFTV_WEB_PORT:-3000}"
PROXY_PORT="${BEEFTV_PROXY_PORT:-8198}"
PYTHON_BIN="${BEEFTV_PYTHON:-./.venv/bin/python3}"
SERVER_BIN="/tmp/beeftv-server"

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

log_info()  { echo -e "${GREEN}[INFO]${NC}  $1"; }
log_warn()  { echo -e "${YELLOW}[WARN]${NC}  $1"; }
log_error() { echo -e "${RED}[ERROR]${NC} $1"; }

# Check ports
check_port() {
    if lsof -i:$1 -sTCP:LISTEN 2>/dev/null | grep -q .; then
        return 0  # port in use
    fi
    return 1  # port free
}

# Stop all services
stop() {
    log_info "Stopping services..."
    pkill -f "beeftv-server" 2>/dev/null || true
    pkill -f "beeftv_proxy.py" 2>/dev/null || true
    pkill -f "vite.*beeftv" 2>/dev/null || true
    log_info "All services stopped."
}

# Start backend
start_backend() {
    if check_port $BACKEND_PORT; then
        log_warn "Backend already running on port $BACKEND_PORT"
        return 0
    fi
    log_info "Building Go backend..."
    cd backend
    go build -o "$SERVER_BIN" ./cmd/server 2>/dev/null || true
    cd ..
    
    log_info "Starting Go backend on :$BACKEND_PORT..."
    CANVAS_BACKEND_DATA_DIR="$BACKEND_DATA" \
    CANVAS_DATABASE_DRIVER=sqlite \
    CANVAS_CORS_ORIGINS="http://localhost:$WEB_PORT" \
    CANVAS_REGISTRATION_ENABLED=true \
    CANVAS_ALLOW_PRIVATE_UPSTREAMS=false \
    CANVAS_ALLOWED_PRIVATE_UPSTREAM_HOSTS="127.0.0.1,localhost,192.168.1.193" \
    CANVAS_BACKEND_ADDR=":$BACKEND_PORT" \
    BEEFTV_PROXY_TOKEN="$BEEFTV_PROXY_TOKEN" \
    "$SERVER_BIN" &
    
    # Wait for backend to be ready
    for i in $(seq 1 10); do
        sleep 2
        if curl -sf http://localhost:$BACKEND_PORT/api/health/live > /dev/null 2>&1; then
            log_info "Backend ready at http://localhost:$BACKEND_PORT"
            return 0
        fi
        echo -n "."
    done
    log_error "Backend failed to start"
    return 1
}

# Start proxy
start_proxy() {
    if check_port $PROXY_PORT; then
        log_warn "Proxy already running on port $PROXY_PORT"
        return 0
    fi
log_info "Starting ComfyUI proxy on :$PROXY_PORT..."
# macOS IPv4-only by default — the `::` dual-stack listener can refuse IPv4
# connections (curl http://127.0.0.1:8198/health → Connection refused) when
# the asyncio task pool is saturated. Force IPv4 unless the caller overrides.
LISTEN_HOST="${BEEFTV_PROXY_LISTEN:-0.0.0.0}"
if [ -n "$BEEFTV_PROXY_TOKEN" ]; then
    BEEFTV_PROXY_TOKEN="$BEEFTV_PROXY_TOKEN" \
        BEEFTV_PROXY_LISTEN="$LISTEN_HOST" \
        "$PYTHON_BIN" beeftv_proxy.py &
else
    BEEFTV_PROXY_LISTEN="$LISTEN_HOST" \
        "$PYTHON_BIN" beeftv_proxy.py &
fi
    
    # Wait for proxy
    for i in $(seq 1 5); do
        sleep 2
        if curl -sf http://localhost:$PROXY_PORT/health > /dev/null 2>&1; then
            log_info "Proxy ready at http://localhost:$PROXY_PORT"
            return 0
        fi
        echo -n "."
    done
    log_error "Proxy failed to start"
    return 1
}

# Start web
start_web() {
    if check_port $WEB_PORT; then
        log_warn "Web already running on port $WEB_PORT"
        return 0
    fi
    log_info "Starting React web on :$WEB_PORT..."
    cd web
    bun run dev &
    cd ..
    
    for i in $(seq 1 10); do
        sleep 3
        if curl -sf http://localhost:$WEB_PORT > /dev/null 2>&1; then
            log_info "Web UI ready at http://localhost:$WEB_PORT"
            return 0
        fi
        echo -n "."
    done
    log_error "Web failed to start"
    return 1
}

# Configure channels
#
# 通道矩阵（DS4 已移除，Ollama qwen3:8b 接管全部文本）：
#   mac-ollama-local → 文本 qwen3:8b（Ollama 本机 11434，经代理）
#   dgx-flux-local   → 图片 flux-schnell（DGX ComfyUI，经代理）
#   dgx-sdxl-local   → 图片 sdxl（DGX ComfyUI，经代理）
#   minimax-h3-local → 视频 minimax-h3（DGX ComfyUI，经代理）
#   mac-piper-local  → 音频 piper-tts（本机 Piper，经代理）
#
# 两个容易踩的坑：
#   1. 代理只监听 IPv4，Go 解析 localhost 优先 ::1 —— baseUrl 必须写 127.0.0.1，
#      否则每个本地生成都会死在 "dial tcp [::1]:8198: connection refused"。
#   2. PUT /workspace/model-config 是写路径，server profile 下必须带登录 token；
#      少了它整段配置会被静默跳过（curl -sf 收到 401 直接返回非零）。
ensure_admin_session() {
    local user="${BEEFTV_ADMIN_USER:-admin}"
    local pass="${BEEFTV_ADMIN_PASS:-}"
    if [ -z "$pass" ]; then
        log_warn "BEEFTV_ADMIN_PASS 未设置 — 跳过通道配置（写路径需要登录会话）"
        return 1
    fi
    local resp
    resp=$(curl -sf -X POST "http://127.0.0.1:$BACKEND_PORT/api/auth/register" \
        -H "Content-Type: application/json" \
        -d "{\"username\":\"$user\",\"password\":\"$pass\"}" 2>/dev/null) || \
    resp=$(curl -sf -X POST "http://127.0.0.1:$BACKEND_PORT/api/auth/login" \
        -H "Content-Type: application/json" \
        -d "{\"username\":\"$user\",\"password\":\"$pass\"}" 2>/dev/null) || {
        log_warn "管理员注册/登录失败"
        return 1
    }
    ADMIN_TOKEN="$(echo "$resp" | python3 -c 'import json,sys; print(json.load(sys.stdin)["data"]["token"])' 2>/dev/null)"
    [ -n "$ADMIN_TOKEN" ] || { log_warn "无法解析管理员 token"; return 1; }
    return 0
}

configure_channels() {
    log_info "配置本地模型通道..."
    # set -e 下函数返回非零会直接中断启动脚本，所以失败只告警不退出。
    ensure_admin_session || { log_warn "没有管理员会话，跳过通道配置（其余服务照常启动）"; return 0; }
    [ -z "$BEEFTV_PROXY_TOKEN" ] && { log_warn "BEEFTV_PROXY_TOKEN 为空 — 代理将不校验凭据"; }
    local proxy="http://127.0.0.1:$PROXY_PORT"
    curl -sf -X PUT "http://127.0.0.1:$BACKEND_PORT/api/workspace/model-config" \
        -H "Content-Type: application/json" \
        -H "Authorization: Bearer $ADMIN_TOKEN" \
        -d '{
            "config": {
                "model": "mac-ollama-local::qwen3:8b",
                "models": [
                    "mac-ollama-local::qwen3:8b",
                    "dgx-flux-local::flux-schnell",
                    "dgx-sdxl-local::sdxl",
                    "minimax-h3-local::minimax-h3",
                    "mac-piper-local::piper-tts"
                ],
                "assistantModel": "mac-ollama-local::qwen3:8b",
                "imageModel": "dgx-flux-local::flux-schnell",
                "imageModels": ["dgx-flux-local::flux-schnell", "dgx-sdxl-local::sdxl"],
                "videoModel": "minimax-h3-local::minimax-h3",
                "audioModel": "mac-piper-local::piper-tts",
                "audioFormat": "mp3",
                "channels": [
                    {
                        "id": "mac-ollama-local", "name": "Qwen3 8B (Ollama)",
                        "baseUrl": "'"$proxy"'", "apiKey": "'"$BEEFTV_PROXY_TOKEN"'", "enabled": true,
                        "scope": "user", "apiFormat": "openai",
                        "models": ["qwen3:8b"],
                        "modelProfiles": [{
                            "model": "qwen3:8b", "displayName": "Qwen3 8B",
                            "capability": "text", "protocol": "chat-completion"
                        }]
                    },
                    {
                        "id": "dgx-flux-local", "name": "FLUX.1 Schnell (DGX Spark)",
                        "baseUrl": "'"$proxy"'", "apiKey": "'"$BEEFTV_PROXY_TOKEN"'", "enabled": true,
                        "scope": "user", "apiFormat": "openai",
                        "models": ["flux-schnell"],
                        "modelProfiles": [{
                            "model": "flux-schnell", "displayName": "FLUX.1 Schnell",
                            "capability": "image", "protocol": "openai-image"
                        }]
                    },
                    {
                        "id": "dgx-sdxl-local", "name": "SDXL Base (DGX Spark)",
                        "baseUrl": "'"$proxy"'", "apiKey": "'"$BEEFTV_PROXY_TOKEN"'", "enabled": true,
                        "scope": "user", "apiFormat": "openai",
                        "models": ["sdxl"],
                        "modelProfiles": [{
                            "model": "sdxl", "displayName": "SDXL Base 1.0",
                            "capability": "image", "protocol": "openai-image"
                        }]
                    },
                    {
                        "id": "minimax-h3-local", "name": "MiniMax H3 (DGX Spark)",
                        "baseUrl": "'"$proxy"'", "apiKey": "'"$BEEFTV_PROXY_TOKEN"'", "enabled": true,
                        "scope": "user", "apiFormat": "openai",
                        "models": ["minimax-h3"],
                        "modelProfiles": [{
                            "model": "minimax-h3", "displayName": "MiniMax H3 T2V",
                            "capability": "video", "protocol": "newapi-channel-2"
                        }]
                    },
                    {
                        "id": "mac-piper-local", "name": "Piper TTS (Mac)",
                        "baseUrl": "'"$proxy"'", "apiKey": "'"$BEEFTV_PROXY_TOKEN"'", "enabled": true,
                        "scope": "user", "apiFormat": "openai",
                        "models": ["piper-tts"],
                        "modelProfiles": [{
                            "model": "piper-tts", "displayName": "Piper TTS",
                            "capability": "audio", "protocol": "openai-audio"
                        }]
                    }
                ]
            }
        }' > /dev/null && log_info "模型通道已配置" || log_warn "通道配置失败"
}

# Main
case "${1:-start}" in
    start)
        stop
        mkdir -p "$BACKEND_DATA"
        start_backend
        start_proxy
        configure_channels
        start_web
        echo ""
        log_info "=================================="
        log_info "BeeFTV Web Edition is ready!"
        log_info "Web UI:  http://localhost:$WEB_PORT"
        log_info "Backend: http://localhost:$BACKEND_PORT"
        log_info "Proxy:   http://localhost:$PROXY_PORT"
        log_info "=================================="
        ;;
    stop)   stop ;;
    restart) stop; sleep 1; $0 start ;;
    status)
        echo "Ports:"
        lsof -i:$BACKEND_PORT 2>/dev/null | grep LISTEN && log_info "Backend OK" || log_warn "Backend DOWN"
        lsof -i:$PROXY_PORT 2>/dev/null | grep LISTEN && log_info "Proxy OK" || log_warn "Proxy DOWN"
        lsof -i:$WEB_PORT 2>/dev/null | grep LISTEN && log_info "Web OK" || log_warn "Web DOWN"
        ;;
    *)
        echo "Usage: $0 {start|stop|restart|status}"
        exit 1 ;;
esac
