#!/usr/bin/env python3
"""A/B one H3 render across ComfyUI attention backends on the DGX Spark.

ComfyUI picks its attention backend once at startup, so the only honest
comparison restarts the server per variant. Both sides are held identical:

  * same seed, prompt, resolution and frame count
  * a warmup render precedes the measured one, so model load and weight
    offload are excluded from the number we report
  * the measured window comes from ComfyUI's own history timestamps, not from
    the proxy, so client-side polling latency cannot inflate or deflate it

Attention only changes the DiT math, so the same graph runs on both sides.
"""
import argparse
import json
import pathlib
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request

DGX = "dgx"
COMFY_DIR = "/home/clarkwei/ComfyUI"
LOG = "/home/clarkwei/.local/share/comfyui-dgx.log"
PY = "./venv/bin/python"
BASE = ["--listen", "0.0.0.0", "--port", "8188", "--bf16-vae",
        "--preview-method", "auto", "--reserve-vram", "4"]
COMFY = "http://192.168.1.193:8188"

# ComfyUI boots with the attention backend baked in from argv.
VARIANTS = {
    "pytorch": BASE,
    "sage": BASE + ["--use-sage-attention"],
}


def ssh(cmd, timeout=120):
    # -n redirects stdin from /dev/null. Without it the remote side can keep the
    # ssh channel's pipe open, so `communicate()` blocks until the long-running
    # child exits instead of returning as soon as the launch is acknowledged.
    return subprocess.run(["ssh", "-n", "-o", "BatchMode=yes",
                           "-o", "ConnectTimeout=20", DGX, cmd],
                          capture_output=True, text=True, timeout=timeout)


def wait_comfy(deadline_s=240):
    t0 = time.time()
    while time.time() - t0 < deadline_s:
        try:
            with urllib.request.urlopen(f"{COMFY}/queue", timeout=8) as r:
                json.load(r)
            return True
        except Exception:
            time.sleep(3)
    return False


def queue_empty():
    with urllib.request.urlopen(f"{COMFY}/queue", timeout=15) as r:
        q = json.load(r)
    return not q.get("queue_running") and not q.get("queue_pending")


def restart_comfy(argv):
    """Restart the DGX ComfyUI with exactly these args.

    The launch goes through a small script on the DGX rather than an inline
    `setsid ... &`. Inlined, the server inherits the ssh channel's stdout and
    `communicate()` blocks until it exits, so every restart looked like a hang.
    The remote script exits as soon as the child is detached.
    """
    quoted = " ".join(argv)
    r = ssh(f"/home/clarkwei/restart_comfy.sh {quoted}", timeout=90)
    if r.returncode != 0:
        raise RuntimeError(f"restart failed rc={r.returncode}: {r.stderr[:200]}")
    if not wait_comfy():
        raise RuntimeError("ComfyUI did not come up")


def active_backend():
    """Ask the running server which backend it actually loaded.

    Reads the tail of the log *after* this restart only, because the log is
    appended to across runs and an older startup would otherwise answer.
    """
    out = ssh(f"tail -80 {LOG} | grep -iE 'Using (sage|pytorch|flash|split|sub quadratic)' "
              f"| tail -1", timeout=30).stdout.strip()
    return re.sub(r"\x1b\[[0-9;]*m", "", out)


def post(prompt, seed):
    graph = json.load(open(pathlib.Path(__file__).parent / "h3_graph.json"))
    graph["7"]["inputs"]["prompt"] = prompt
    # The seed reaches the sampler through node 8 (PrimitiveInt) -> 9
    # (RandomNoise); node 7 has no seed input of its own.
    graph["8"]["inputs"]["value"] = seed
    body = json.dumps({"prompt": graph, "client_id": "h3-ab"}).encode()
    req = urllib.request.Request(COMFY + "/prompt", data=body,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.load(r)["prompt_id"]


def run_one(prompt, seed, tag):
    """Queue one render and time it from ComfyUI's own history record.

    Timing must span at least two distinct timestamps. A prompt whose nodes are
    all served from ComfyUI's execution cache reports a single message and
    computes as 0.0s, which would silently poison an A/B: both sides would read
    "instant" and the ratio would be meaningless. `wait_for_uncached` runs a
    throwaway prompt first so the measured render actually does the work.
    """
    pid = post(prompt, seed)
    deadline = time.time() + 3600
    while time.time() < deadline:
        time.sleep(10)
        try:
            with urllib.request.urlopen(f"{COMFY}/history/{pid}", timeout=20) as r:
                h = json.load(r)
        except Exception:
            continue
        if pid not in h:
            continue
        e = h[pid]
        msgs = e["status"]["messages"]
        ts = [m[1]["timestamp"] for m in msgs
              if isinstance(m[1], dict) and m[1].get("timestamp")]
        dur = (max(ts) - min(ts)) / 1000 if len(ts) >= 2 else 0.0
        cached = any(m[0] == "execution_cached" for m in msgs)
        return {"tag": tag, "prompt_id": pid, "duration_s": round(dur, 1),
                "status": e["status"]["status_str"], "fully_cached": cached,
                "n_messages": len(msgs),
                "backend_line": active_backend()}


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--variants", default="pytorch,sage")
    ap.add_argument("--prompt", default="a paper boat drifting on a calm lake at sunset, gentle ripples, cinematic")
    ap.add_argument("--seed", type=int, default=1234)
    ap.add_argument("--out", default="attn-ab.json")
    a = ap.parse_args()

    results = []
    for name in a.variants.split(","):
        print(f"\n=== {name} ===", flush=True)
        restart_comfy(VARIANTS[name])
        print(f"  backend: {active_backend()}", flush=True)
        print("  warmup...", flush=True)
        run_one(a.prompt, a.seed, "warmup")          # loads weights, excluded
        wait_comfy(); time.sleep(15)
        # A different seed forces the sampler to actually recompute. Reusing the
        # warmup seed lets ComfyUI serve the whole graph from its execution
        # cache, which reports no work and would compare as "instant".
        r = run_one(a.prompt, a.seed + 1, "measured")
        if r["duration_s"] <= 0:
            raise RuntimeError(
                f"{name}: measured render reported {r['duration_s']}s "
                f"(fully_cached={r['fully_cached']}); refusing to record a "
                "meaningless speedup")
        print(f"  {r['duration_s']}s  {r['status']}", flush=True)
        results.append({"variant": name, **r})
        out = pathlib.Path(__file__).parent / a.out
        out.write_text(json.dumps(results, indent=2))

    print("\n=== RESULT ===")
    for r in results:
        print(f"  {r['variant']:<10} {r['duration_s']:>7}s  {r['status']}")
    if len(results) == 2:
        a_, b_ = results[0]["duration_s"], results[1]["duration_s"]
        print(f"  speedup {a_ / b_:.3f}x")
    print("DONE")