#!/usr/bin/env python3
"""Validate accel graph variants against a live ComfyUI without rendering.

ComfyUI's /prompt endpoint validates node classes, input names, combo options
and link targets *before* it queues anything. A graph rejected here never
reaches the GPU, which makes this a safe, free check — but ONLY if the body
is rejected. A graph that validates is really queued and will render, so this
script must not be run against a shared GPU.

Kept in scripts/ rather than inlined so the five variants can be re-verified
after any graph edit instead of being re-derived by hand.
"""
import json
import os
import pathlib
import sys
import urllib.error
import urllib.request

COMFY = os.environ.get("COMFY", "http://192.168.1.193:8188")
PROXY_PY = pathlib.Path(__file__).resolve().parent.parent / "beeftv_proxy.py"

VARIANTS = {
    "baseline (int8)": {},
    "fp8 unet": {"BEEFTV_H3_UNET": "minimax_h3_fl2va_pruned_fp8_scaled.safetensors"},
    "easycache 0.2": {"BEEFTV_H3_EASYCACHE_THRESHOLD": "0.2"},
    "torch.compile": {"BEEFTV_H3_TORCH_COMPILE": "1"},
    "easycache + torch.compile": {
        "BEEFTV_H3_EASYCACHE_THRESHOLD": "0.2",
        "BEEFTV_H3_TORCH_COMPILE": "1",
    },
}


def build(env):
    for k in [k for k in os.environ if k.startswith("BEEFTV_H3_")]:
        os.environ.pop(k)
    os.environ.update(env)
    import importlib.util

    spec = importlib.util.spec_from_file_location("bp", PROXY_PY)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.build_minimax_h3_workflow("graph preflight, do not render")


def main():
    ok = True
    for tag, env in VARIANTS.items():
        graph = build(env)
        body = json.dumps({"prompt": graph, "client_id": "accel-preflight"}).encode()
        req = urllib.request.Request(
            COMFY + "/prompt", data=body, headers={"Content-Type": "application/json"}
        )
        try:
            urllib.request.urlopen(req, timeout=60).read()
            print(f"  PASS  {tag}")
        except urllib.error.HTTPError as e:
            ok = False
            print(f"  FAIL  {tag}: HTTP {e.code} {e.read().decode()[:400]}")
    print("ALL PASS" if ok else "SOME FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())