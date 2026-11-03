# BeefTV Public Access — Architecture & Setup Guide

## TL;DR

Use **Cloudflare Tunnel** to expose only the web UI publicly, gated by **Cloudflare Access** (email allowlist). The backend (8080) and proxy (8198) stay on `localhost` and are **never reachable from the internet**. A shared `BEEFTV_PROXY_TOKEN` protects GPU/model endpoints.

---

## Threat Model

| Actor | Can do | Cannot do |
|---|---|---|
| Random stranger (internet) | See the Cloudflare login page | Access any content |
| Authenticated user (owner email) | Full web UI + model generation | Nothing blocked |
| The owner on cellular/any device | Full access | Nothing blocked |
| Internal service (`localhost`) | Everything | — |

---

## Architecture

```
Internet ────► Cloudflare Edge ────► [Access Policy: email allowlist]
                     │
                     ▼
              Cloudflare Tunnel
              (cloudflared)
                     │
                     ▼
              http://localhost:3000  ◄── Web UI (Vite dev server)
                     │
                     │  browser requests
                     ▼
              http://localhost:8080   ◄── Go backend (never public)
                     │
                     │  /v1/* with Authorization: Bearer <token>
                     ▼
              http://localhost:8198   ◄── beeftv_proxy.py
                                          │
                                          │  internal LAN
                                          ▼
                                   192.168.1.193:8188  DGX Spark ComfyUI
                                   192.168.1.193:8000  DGX Spark DeepSeek V4
```

**Key property**: `localhost:8080` and `localhost:8198` are bound to loopback only. No router NAT, no port forwarding, no `0.0.0.0` binding for them.

---

## Access Layers

### Layer 1 — Cloudflare Access (edge gate)

- Free plan supports email OTP allowlist for 3 apps.
- Every request to `https://beeftv.<your-domain>` hits Cloudflare first.
- Cloudflare presents a login page → user enters their email → receives OTP → approved.
- Everyone else: **blocked at the edge**. No request reaches the web server.

### Layer 2 — Backend registration lockout

```bash
CANVAS_REGISTRATION_ENABLED=false   # ← no new accounts, no random sign-ups
CANVAS_CORS_ORIGINS=https://beeftv.<your-domain>  # ← web origin only
CANVAS_ALLOW_PRIVATE_UPSTREAMS=false # ← no SSRF to 192.168.x.x from backend
```

### Layer 3 — Proxy token (model bridge gate)

`beeftv_proxy.py` at `:8198` now requires a shared secret on all `/v1/*` routes:

```bash
# Generate
openssl rand -hex 32

# Set
export BEEFTV_PROXY_TOKEN="<output>"

# Start
BEEFTV_PROXY_TOKEN="$BEEFTV_PROXY_TOKEN" python beeftv_proxy.py
```

The Go backend forwards `Authorization: Bearer <token>` (already its native auth format) automatically — **no code changes needed** in the backend.

The browser → proxy leg also works: `X-Beeftv-Token: <token>` header is accepted.

### Layer 4 — Proxy CORS hardening

- `Access-Control-Allow-Origin` restricted to the configured public origin (not `*`).
- `/health` and `/view` remain open (no token needed, CORS still restricted).
- Framing protection (`X-Frame-Options: DENY`) on all responses.

---

## Setup: Step by Step

### 1. Prerequisites

```bash
# Cloudflared is already installed
cloudflared --version
# 2026.3.0

# Verify domain is on Cloudflare DNS
# (your domain must be delegated to Cloudflare)
```

### 2. Create a Cloudflare Tunnel

```bash
cloudflared tunnel create beeftv-local
# Output: Created tunnel beeftv-local with id a1b2c3d4-...

# Note the TUNNEL ID from the output
```

### 3. Configure DNS

In Cloudflare Dashboard → your domain → DNS → Records:

| Type | Name | Target | Proxy |
|---|---|---|---|
| CNAME | beeftv | `a1b2c3d4-...` | Proxied |

Or via CLI:
```bash
cloudflared tunnel route dns beeftv-local beeftv.<your-domain>
```

### 4. Fill in the tunnel config

Edit `deploy/cloudflared/config.yml`:

```yaml
tunnel: a1b2c3d4-5678-...    # ← your tunnel ID
credentials-file: /Users/yahweh/.cloudflared/a1b2c3d4-....json
ingress:
  - hostname: beeftv.<your-domain>   # ← your real domain
    service: http://localhost:3000
```

### 5. Configure Cloudflare Access

In [dash.cloudflare.com → Zero Trust](https://dash.cloudflare.com/zero-trust):

1. **Networks → Tunnels**: verify your tunnel shows "Active".
2. **Access → Applications → Add an application → Self-hosted**:
   - Application name: `beeftv-local`
   - Application domain: `beeftv.<your-domain>`
   - Session duration: `24 hours`
3. **Add a policy**:
   - Policy name: `Owner`
   - Action: `Allow`
   - Include: **Emails** → add `your@email.com`
4. Default rule (`Block`) already exists and blocks everyone else.

### 6. Generate the proxy token

```bash
./deploy/scripts/start-public.sh token
# Or:
openssl rand -hex 32
```

Add to your shell profile (e.g. `~/.zshrc`):
```bash
export BEEFTV_PROXY_TOKEN="<your-token>"
```

### 7. Start the public deployment

```bash
./deploy/scripts/start-public.sh start
```

Expected output:
```
[INFO]  Backend ready at http://localhost:8080
[INFO]  Proxy ready at http://localhost:8198
[INFO]  Web UI ready at http://localhost:3000
[TUNNEL] Cloudflare Tunnel started (PID ...)
[TUNNEL] Public URL: https://beeftv.<YOUR_DOMAIN>
```

### 8. Verify

**From any device outside your LAN:**
```bash
# Without auth → blocked by Cloudflare Access
curl -sI https://beeftv.<your-domain> | head -5
# Expected: HTTP 302 → Cloudflare login page

# After authenticating via email OTP, should reach the web UI
```

**Health check (local):**
```bash
curl http://localhost:8198/health
# { "status": "ok", ... }

# Verify token gate:
curl http://localhost:8198/v1/models
# Without token: HTTP 401

curl -H "Authorization: Bearer <token>" http://localhost:8198/v1/models
# With token: HTTP 200, model list
```

---

## Files Created

```
deploy/
  cloudflared/
    config.yml              # Tunnel ingress config (fill in tunnel ID + domain)
  cloudflare-access/
    policy.json             # Zero Trust allowlist policy (template)
  scripts/
    start-public.sh         # Orchestrates: backend + proxy + web + tunnel
    token                   # (generated) secret for proxy auth

beeftv_proxy.py             # Modified: auth middleware + hardened CORS
start.sh                    # Modified: hardened defaults (reg=false, SSRF=false)
```

---

## Environment Variables

| Variable | Default | Purpose |
|---|---|---|
| `BEEFTV_PROXY_TOKEN` | _(empty)_ | Shared secret for `/v1/*` auth |
| `BEEFTV_PROXY_ALLOWED_ORIGINS` | `http://localhost:3000` | Comma-separated CORS origins |
| `CANVAS_REGISTRATION_ENABLED` | `false` | Block new user registrations |
| `CANVAS_ALLOW_PRIVATE_UPSTREAMS` | `false` | Block SSRF to private network |
| `CANVAS_CORS_ORIGINS` | _(set by script)_ | Web origin only |

---

## Mobile / Cellular Access

Cloudflare Access works from **any device with a browser**. Steps on a new device:

1. Open `https://beeftv.<your-domain>`
2. Cloudflare shows the login screen
3. Enter your registered email
4. Receive OTP → enter it
5. You're in, full access

No app install, no VPN, no port forwarding required.

---

## Rollback (if needed)

```bash
# Stop everything
./deploy/scripts/start-public.sh stop

# Resume normal local-only usage
./start.sh start
```

---

## Security Notes

- **Token in frontend bundle**: `BEEFTV_PROXY_TOKEN` is read server-side from the environment and forwarded by the Go backend via `Authorization: Bearer`. The browser never needs to store or send it directly (except for non-Backend calls like `/v1/models` direct from the browser). If the web UI makes direct proxy calls, add the `X-Beeftv-Token` header at the service layer.
- **No secrets in git**: The `deploy/cloudflared/config.yml` uses placeholder values. The actual credentials are in `~/.cloudflared/`.
- **No port forwarding**: The Mac Studio has no inbound ports open. All outbound tunnel traffic is established by `cloudflared` running locally.
- **DGX Spark stays private**: `192.168.1.193` is never exposed. Only `beeftv_proxy.py` (on localhost) can reach it.
