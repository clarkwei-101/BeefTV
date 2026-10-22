#!/usr/bin/env python3
"""Benchmark MiniMax H3 accel variants on the DGX Spark, end to end.

Each variant is a (checkpoint, extra graph nodes) pair applied to the *same*
H3 graph, so the only thing that differs between rows is the variable under
test. Timing comes from ComfyUI's own history timestamps rather than the proxy
poll loop, and every variant gets a warmup render first, because H3 loads
~22 GB and a cold first render would otherwise dominate the number.

Rejected outright rather than measured:
  * EasyCache -- corrupts the H3 audio branch while video SSIM stays ~0.95,
    so the damage is invisible in review. The proxy refuses it unless
    BEEFTV_H3_EASYCACHE_ALLOW_AUDIO_LOSS=1.
  * Wan 2.1 -- no weights on this host (models/wan_i2v is empty); the proxy
    aliases wan requests to H3, so there is nothing to accelerate.

Usage:
    python3 scripts/bench_h3_variants.py --variants int8,int8_compile
"""
import argparse
import json
import pathlib
import subprocess
import time
import urllib.request

HERE = pathlib.Path(__file__).parent
COMFY = "http://192.168.1.193:8188"
RESTART = "/home/clarkwei/restart_comfy.sh"
DEFAULT_ARGS = ("--listen 0.0.0.0 --port 8188 --bf16-vae "
                "--preview-method auto --reserve-vram 4")

H3_UNET_INT8 = "minimax_h3_fl2va_pruned_int8_convrot.safetensors"
# The fp8 build on this host is named *_fp8_scaled -- there is no fp8_convrot.
H3_UNET_FP8 = "minimax_h3_fl2va_pruned_fp8_scaled.safetensors"
H3_CLIP = "qwen3vl_32b_minimax_h3_int8_convrot.safetensors"


def ssh(cmd, timeout=120):
    return subprocess.run(
        ["ssh", "-n", "-o", "BatchMode=yes", "-o", "ConnectTimeout=20", "dgx", cmd],
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


def has_checkpoint(name):
    """A missing checkpoint would fail 20s into the render, not at submit."""
    out = ssh(f"ls /home/clarkwei/ComfyUI/models/diffusion_models/{name} 2>/dev/null",
              timeout=30)
    return bool(out.stdout.strip())


def build_graph(prompt, seed, unet, compile_=False):
    """Rebuild through the proxy so the tested graph is the shipped graph."""
    import importlib.util
    os_env = {"BEEFTV_H3_TORCH_COMPILE": "1"} if compile_ else {}
    for k, v in os_env.items():
        import os
        os.environ[k] = v
    import os
    os.environ["BEEFTV_H3_UNET"] = unet
    spec = importlib.util.spec_from_file_location("bp", HERE.parent / "beeftv_proxy.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    g = m.build_minimax_h3_workflow(prompt)
    for k in os_env:
        os.environ.pop(k, None)
    os.environ.pop("BEEFTV_H3_UNET", None)
    g["7"]["inputs"]["prompt"] = prompt
    g["8"]["inputs"]["value"] = seed
    return g


def post(graph):
    body = json.dumps({"prompt": graph, "client_id": "h3-var"}).encode()
    req = urllib.request.Request(COMFY + "/prompt", data=body,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.load(r)["prompt_id"]


def run_once(prompt, seed, unet, compile_, tag):
    pid = post(build_graph(prompt, seed, unet, compile_))
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
        return {"tag": tag, "duration_s": round(dur, 1),
                "status": e["status"]["status_str"], "n_messages": len(msgs)}


VARIANTS = {
    # name            unet checkpoint        torch.compile
    "int8":          (H3_UNET_INT8, False),
    "fp8":           (H3_UNET_FP8, False),
    "int8_compile":  (H3_UNET_INT8, True),
    "fp8_compile":   (H3_UNET_FP8, True),
}


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--variants", default="int8")
    ap.add_argument("--prompt",
                    default="a paper boat drifting on a calm lake at sunset, gentle ripples, cinematic")
    ap.add_argument("--seed", type=int, default=1234)
    ap.add_argument("--out", default="h3-variants.json")
    a = ap.parse_args()

    results = []
    for name in a.variants.split(","):
        unet, comp = VARIANTS[name]
        print(f"\n=== {name} (unet={unet}, compile={comp}) ===", flush=True)
        if not has_checkpoint(unet):
            print(f"  SKIP: {unet} not on this host", flush=True)
            results.append({"variant": name, "skipped": f"{unet} missing"})
            continue

        r = ssh(f"{RESTART} {DEFAULT_ARGS}", timeout=90)
        if r.returncode != 0 or not wait_comfy():
            raise RuntimeError("ComfyUI restart failed")

        print("  warmup...", flush=True)
        w = run_once(a.prompt, a.seed, unet, comp, "warmup")
        print(f"  warmup {w['duration_s']}s {w['status']}", flush=True)
        # Different seed: reusing it lets ComfyUI serve the graph from its
        # execution cache and report ~0s, which compares as "infinitely fast".
        m = run_once(a.prompt, a.seed + 1, unet, comp, "measured")
        if m["duration_s"] <= 0:
            raise RuntimeError(f"{name}: measured render was cache-served (0.0s)")
        print(f"  measured {m['duration_s']}s {m['status']}", flush=True)
        results.append({"variant": name, "unet": unet, "torch_compile": comp, **m})
        (HERE / a.out).write_text(json.dumps(results, indent=2))

    print("\n=== RESULT ===")
    base = next((r for r in results if r.get("variant") == "int8"), None)
    for r in results:
        if r.get("skipped"):
            print(f"  {r['variant']:<14} SKIP ({r['skipped']})")
            continue
        sp = f"  {base['duration_s'] / r['duration_s']:.3f}x" if base and r.get("duration_s") else ""
        print(f"  {r['variant']:<14} {r['duration_s']:>7}s{sp}")
    print("DONE")