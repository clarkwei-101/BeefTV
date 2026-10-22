#!/usr/bin/env python3
"""Measure MiniMax H3 render wall-clock per acceleration variant.

Each variant is a fresh proxy process with a different environment, because
the switches are read per render but the ComfyUI-side model cache must be
emptied between runs or run N inherits run N-1's warm weights and the numbers
stop being comparable.

Reports two numbers per run:
  submit  - POST /v1/video/generations -> accepted (202)  (pure dispatch cost)
  total   - accepted -> output file downloaded and probed
and saves each output so the ffprobe size/duration can confirm the runs are
actually comparable rather than silently different lengths.
"""
import argparse, json, os, pathlib, subprocess, sys, time, urllib.error, urllib.request

PROXY = os.environ.get("BEEFTV_PROXY", "http://127.0.0.1:8198")
TOKEN = os.environ.get("BEEFTV_PROXY_TOKEN", "")
OUT = pathlib.Path(__file__).resolve().parent.parent / ".local" / "accel-bench"
PROXY_PY = pathlib.Path(__file__).resolve().parent.parent / "beeftv_proxy.py"
COMFY = os.environ.get("COMFY", "http://192.168.1.193:8188")

# (label, extra env). "baseline" must stay the known-good int8 default.
VARIANTS = {
    "baseline-int8":     {},
    "fp8-unet":          {"BEEFTV_H3_UNET": "minimax_h3_fl2va_pruned_fp8_scaled.safetensors"},
    "easycache-0.2":     {"BEEFTV_H3_EASYCACHE_THRESHOLD": "0.2"},
    "torch-compile":     {"BEEFTV_H3_TORCH_COMPILE": "1"},
    "easycache+compile": {"BEEFTV_H3_EASYCACHE_THRESHOLD": "0.2", "BEEFTV_H3_TORCH_COMPILE": "1"},
}


def http_json(url, payload=None, headers=None, timeout=60):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, headers=headers or {})
    if data:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = r.read()
            return r.status, (json.loads(body) if body else {})
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()[:500]


def wait_comfy_idle(timeout=3600):
    """Block until the DGX queue drains, so each variant starts cold."""
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            with urllib.request.urlopen(f"{COMFY}/queue", timeout=15) as r:
                q = json.load(r)
            if not q.get("queue_running") and not q.get("queue_pending"):
                return True
        except Exception:
            pass
        time.sleep(5)
    return False


def probe(path):
    if not path.exists():
        return None
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0", "-count_frames",
             "-show_entries", "stream=width,height,nb_read_frames",
             "-show_entries", "format=duration", "-of", "json", str(path)],
            capture_output=True, text=True, timeout=120)
        return json.loads(out.stdout)
    except Exception:
        return None


def run_variant(label, env_extra, args):
    OUT.mkdir(parents=True, exist_ok=True)
    env = {**os.environ, "BEEFTV_PROXY_LISTEN": "0.0.0.0", **env_extra}
    log = open(OUT / f"bench_{label}.log", "wb")
    proc = subprocess.Popen([sys.executable, str(PROXY_PY)], env=env,
                            stdout=log, stderr=subprocess.STDOUT)
    try:
        # Wait for the port rather than sleeping a guessed interval.
        for _ in range(60):
            try:
                if http_json(f"{PROXY}/health", timeout=3)[0] == 200:
                    break
            except Exception:
                time.sleep(1)
        wait_comfy_idle()
        http_json(f"{COMFY}/free",
                  {"unload_models": True, "free_memory": True})
        time.sleep(10)

        body = {
            "model": args.model, "prompt": args.prompt,
            "size": args.size, "n": 1, "response_format": "url",
        }
        headers = {"X-Beeftv-Token": TOKEN} if TOKEN else {}
        t0 = time.time()
        status, resp = http_json(f"{PROXY}/v1/video/generations", body,
                                 {**headers, "Content-Type": "application/json"},
                                 timeout=args.timeout)
        submit = time.time() - t0
        if status != 202:
            return {"variant": label, "ok": False, "submit_s": round(submit, 1),
                    "error": f"HTTP {status}: {str(resp)[:300]}"}
        job_id = resp["id"]
        poll = f"{PROXY}/v1/video/generations/{job_id}"

        last = None
        while time.time() - t0 < args.timeout:
            time.sleep(10)
            st, r = http_json(poll, headers=headers, timeout=30)
            if st != 200:
                continue
            cur = r.get("status")
            if cur != last:
                print(f"    [{label}] {int(time.time()-t0):>5}s  {cur}", flush=True)
                last = cur
            if cur in ("completed", "succeeded", "success"):
                break
            if cur in ("failed", "error", "cancelled"):
                return {"variant": label, "ok": False,
                        "submit_s": round(submit, 1),
                        "total_s": round(time.time() - t0, 1),
                        "error": str(r)[:300]}
        total = time.time() - t0
        url = r.get("data", [{}])[0].get("url") or r.get("url")
        media = OUT / f"bench_{label}.mp4"
        if url:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=900) as resp, open(media, "wb") as fh:
                shutil_copy(resp, fh)
        return {"variant": label, "ok": True, "submit_s": round(submit, 2),
                "total_s": round(total, 1), "bytes": media.stat().st_size if media.exists() else 0,
                "probe": probe(media), "media": str(media),
                "env": env_extra}
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=20)
        except subprocess.TimeoutExpired:
            proc.kill()
        log.close()
        subprocess.run(["pkill", "-f", "beeftv_proxy.py"], capture_output=True)
        time.sleep(3)


def shutil_copy(src, dst, cap=2 << 30):
    import shutil
    shutil.copyfileobj(src, dst, cap)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--variants", default="all")
    ap.add_argument("--model", default="minimax-h3")
    ap.add_argument("--size", default="1344x768")
    ap.add_argument("--prompt", default="a paper boat drifting on a calm lake at sunset, gentle ripples, cinematic")
    ap.add_argument("--timeout", type=int, default=2400)
    ap.add_argument("--out", default="bench-results.json")
    a = ap.parse_args()

    todo = list(VARIANTS) if a.variants == "all" else a.variants.split(",")
    OUT.mkdir(parents=True, exist_ok=True)
    results = []
    for label in todo:
        print(f"\n=== {label} ===", flush=True)
        r = run_variant(label, VARIANTS[label], a)
        results.append(r)
        print(json.dumps(r, ensure_ascii=False)[:600], flush=True)
        (OUT / a.out).write_text(json.dumps(results, indent=2))
    print("\nDONE ->", OUT / a.out)