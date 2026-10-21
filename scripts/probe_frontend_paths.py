#!/usr/bin/env python3
"""Probe every backend path the BeefTV web client actually calls.

The frontend route table is the contract we care about: if the UI calls
`GET /api/ai/models` and the backend answers 404, that button is dead on
arrival. This walks the real path list and reports the live status of each.

Paths are probed with the method the client uses. A 404 on a path that the
UI calls is a hard contract break; 401/403 are auth gating (expected for
the local profile); 405 on a known path means the method is wrong.
"""
import json
import urllib.error
import urllib.request

import os

BASE = os.environ.get("BEEFTV_BACKEND_BASE", "http://127.0.0.1:8081").rstrip("/") + "/api"

# (method, path) pairs taken from web/src/services/api/*.ts
CALLS = [
    ("GET", "/agent-clients"),
    ("POST", "/ai/models"),
    ("GET", "/asset-folders"),
    ("GET", "/assets"),
    ("POST", "/assets/batch"),
    ("PATCH", "/assets/folder"),
    ("POST", "/assistant/host/restart"),
    ("GET", "/assistant/sessions"),
    ("POST", "/assistant/sessions/activate"),
    ("GET", "/assistant/status"),
    ("POST", "/assistant/ui-session"),
    ("POST", "/audio/speech"),
    ("POST", "/audio/tasks"),
    ("GET", "/beefapi/connection"),
    ("POST", "/beefapi/connection/cancel"),
    ("POST", "/beefapi/connection/disconnect"),
    ("POST", "/beefapi/connection/open-wallet"),
    ("POST", "/beefapi/connection/start"),
    ("GET", "/canvas-folders"),
    ("GET", "/canvas-projects"),
    ("GET", "/creation-conversations"),
    ("POST", "/creation-conversations/import"),
    ("GET", "/creation-runs"),
    ("POST", "/depth-captures"),
    ("GET", "/plugins"),
    ("GET", "/plugins/catalog"),
    ("GET", "/plugins/eagle/items"),
    ("GET", "/plugins/eagle/library"),
    ("GET", "/plugins/status"),
    ("GET", "/project-folders"),
    ("GET", "/projects"),
    ("GET", "/public/appearance"),
    ("GET", "/resources"),
    ("POST", "/resources/import"),
    ("GET", "/resources/storage-usage"),
    ("POST", "/resources/uploads"),
    ("POST", "/runninghub/app-info"),
    ("POST", "/runninghub/workflow-info"),
    ("GET", "/settings/prompt-templates"),
    ("GET", "/skills"),
    ("GET", "/skills/added"),
    ("POST", "/skills/install"),
    ("POST", "/skills/install/github"),
    ("GET", "/style-profiles"),
    ("GET", "/tasks"),
    ("POST", "/timeline/render-plan"),
    ("POST", "/timeline/renders"),
    ("POST", "/timeline/transcriptions"),
]


def probe(method, path):
    url = BASE + path
    data = b"{}" if method in ("POST", "PUT", "PATCH") else None
    req = urllib.request.Request(url, data=data, method=method)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=8) as resp:
            return resp.status, resp.read(160).decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read(160).decode("utf-8", "replace")
    except Exception as exc:  # noqa: BLE001
        return 0, f"{type(exc).__name__}: {exc}"


broken = []
for method, path in CALLS:
    status, body = probe(method, path)
    flag = ""
    if status == 404:
        flag = "  <-- CONTRACT BREAK (path not served)"
        broken.append((method, path, status, body))
    elif status == 0:
        flag = "  <-- CONNECTION ERROR"
        broken.append((method, path, status, body))
    elif status in (401, 403):
        flag = "  (auth gated)"
    print(f"{status:>3} {method:5s} {path:42s} {body[:70]}{flag}")

print()
if broken:
    print(f"=== {len(broken)} frontend-called path(s) are NOT served by the backend ===")
    for method, path, status, body in broken:
        print(f"  {method} {path} -> {status} {body[:100]}")
else:
    print("=== every frontend-called path is served ===")
