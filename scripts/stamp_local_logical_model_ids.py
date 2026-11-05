#!/usr/bin/env python3
"""Stamp logicalModelId onto local-channel model profiles.

The frontend resolves a task's routing id via
    logicalModelIDForConfig(config) -> modelProfiles[].logicalModelId
and only sends it as `logicalModelId` on the task request. Without it the
backend falls through to the system-channel lookup, which needs a
`channelId`; local channels are user-scoped so the frontend sends an empty
channelId and the backend rejects the task with
"必须指定系统渠道和模型" (must specify system channel and model).

The backend's resolveLocalModelSelection() understands exactly these ids:
    <channelId>::<modelKey>
so the profile must carry the same "channelId::model" string.
"""
import json
import sys

LOCAL_CHANNELS = {
    "dgx-flux-local",
    "dgx-sdxl-local",
    "minimax-h3-local",
    "dgx-minimax-local",
    "dgx-wan-local",
    "mac-ollama-local",
    "mac-piper-local",
    # 后端 resolveLocalModelSelection() 的 switch 里没有 dgx-deepseek-local，
    # 它会落到 default 分支返回 false，于是走系统渠道解析并因 channelId 为空
    # 被拒。这里仍然打上 logicalModelId，同时补齐后端的 channel 白名单。
    "dgx-deepseek-local",
}

path = sys.argv[1]
with open(path, encoding="utf-8") as fh:
    doc = json.load(fh)

config = doc.get("config", doc)
changed = 0
for channel in config.get("channels", []):
    cid = channel.get("id")
    if cid not in LOCAL_CHANNELS:
        continue
    for profile in channel.get("modelProfiles") or []:
        model = (profile.get("model") or "").strip()
        if not model:
            continue
        expected = f"{cid}::{model}"
        if profile.get("logicalModelId") != expected:
            profile["logicalModelId"] = expected
            changed += 1
            print(f"  {cid}::{model} -> logicalModelId={expected}")

with open(path, "w", encoding="utf-8") as fh:
    json.dump(doc, fh, ensure_ascii=False, indent=2)

print(f"patched {changed} model profiles in {path}")
