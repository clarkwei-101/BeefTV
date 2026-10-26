"""
OpenAI-Compatible Model Bridge for BeefTV.

Fans a single OpenAI-shaped endpoint out over every model the workstation owns,
split across two machines:

  * DGX Spark (192.168.1.193) — ComfyUI for image (FLUX.1 Schnell, SDXL) and
    video (MiniMax H3), plus DeepSeek V4 Flash for long-form text.
  * Mac Studio (this host) — Ollama for short text, Piper for speech, and a
    second ComfyUI for image work when the DGX queue is busy.

Usage:
    python beeftv_proxy.py

Endpoints:
    POST /v1/chat/completions     → text routing, or video generation
    POST /v1/images/generations   → FLUX.1 Schnell / SDXL txt2img + img2img
    POST /v1/images/edits         → img2img
    POST /v1/audio/speech         → Piper TTS
    GET  /v1/models               → catalogue of every routable model
    GET  /health                  → per-backend reachability
    GET  /view                    → streams ComfyUI output bytes
"""

import asyncio
import base64
import functools
import json
import logging
import os
import re
import secrets
import shutil
import subprocess
import tempfile
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, AsyncGenerator, Dict, List, Optional, Union
from urllib.parse import quote

import httpx
from fastapi import FastAPI, File, Form, HTTPException, Request, Response, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("beeftv-proxy")

# ── Configuration ────────────────────────────────────────────────────────────────
COMFY_HOST = os.environ.get("BEEFTV_DGX_COMFY", "http://192.168.1.193:8188")
PORT = int(os.environ.get("BEEFTV_PROXY_PORT", "8198"))
# Listen address. macOS default to IPv4: a "::" dual-stack listener can refuse
# IPv4 connections (curl http://127.0.0.1:8198/health -> Connection refused)
# once the asyncio task pool is saturated.
LISTEN = os.environ.get("BEEFTV_PROXY_LISTEN", "0.0.0.0")
# The base URL handed back to callers for generated media. Override when the
# BeefTV web app is opened from a machine other than this one.
#
# Must be an IPv4 literal, NOT "localhost": Go resolves "localhost" to ::1
# first, and the backend then fails with
#   dial tcp [::1]:8198: connect: connection refused
# when it downloads the media URL we hand back.
PUBLIC_BASE = os.environ.get("BEEFTV_PROXY_PUBLIC_BASE", f"http://127.0.0.1:{PORT}")
# Shared secret for proxy authentication. If set, every /v1/* request must carry
# this value in the X-Beeftv-Token header. Leave blank to disable (dev only).
PROXY_TOKEN = os.environ.get("BEEFTV_PROXY_TOKEN", "")
# Allowed browser origins for CORS. Commas-separated list. Defaults to localhost
# for development; set to your public tunnel origin for production.
ALLOWED_ORIGINS = [
    o.strip()
    for o in os.environ.get("BEEFTV_PROXY_ALLOWED_ORIGINS", "").split(",")
    if o.strip()
] or ["http://localhost:3000", "http://localhost:5173"]

# ── Text routing ────────────────────────────────────────────────────────────────
# ALL text goes to the Mac's Ollama (qwen3:8b). The DGX is reserved for
# ComfyUI image/video — no DeepSeek leg (removed 2026-10-26, DS4 decommissioned).
OLLAMA_URL = os.environ.get("BEEFTV_OLLAMA_URL", "http://127.0.0.1:11434")
OLLAMA_MODEL = os.environ.get("BEEFTV_OLLAMA_MODEL", "qwen3:8b")
OLLAMA_TIMEOUT = int(os.environ.get("BEEFTV_OLLAMA_TIMEOUT", "180"))

# ── Speech routing ──────────────────────────────────────────────────────────────
PIPER_BIN = os.environ.get(
    "BEEFTV_PIPER_BIN",
    os.path.expanduser(
        "~/Library/Application Support/com.pollen-robotics.reachy-mini"
        "/apps_venv/bin/piper"
    ),
)
PIPER_VOICE_DIR = Path(
    os.environ.get(
        "BEEFTV_PIPER_VOICE_DIR",
        os.path.expanduser(
            "~/Library/Application Support/com.pollen-robotics.reachy-mini"
            "/apps_venv/lib/python3.12/site-packages/reachy_voice_agent/models/piper"
        ),
    )
)
DEFAULT_VOICE = os.environ.get("BEEFTV_DEFAULT_VOICE", "zh_CN-huayan-medium")
FFMPEG_BIN = os.environ.get("BEEFTV_FFMPEG", "ffmpeg")

# Piper's pinyin voices pull g2pW, which in turn pulls a BERT tokenizer from
# Hugging Face. The Reachy venv ships a transformers new enough to call an HF
# endpoint that now answers 401, so the download must be pre-cached and the
# run forced offline. Piper also writes g2pW into its working directory, which
# is why it gets an explicit cwd instead of inheriting the server's.
PIPER_CACHE_DIR = Path(
    os.environ.get("BEEFTV_PIPER_CACHE", os.path.expanduser("~/.cache/beeftv-piper"))
)
PIPER_ENV = {
    **os.environ,
    "HF_HUB_OFFLINE": "1",
    "TRANSFORMERS_OFFLINE": "1",
    "HF_HOME": os.environ.get("HF_HOME", os.path.expanduser("~/.cache/huggingface")),
    # g2pW resolves a Chinese BERT and a word-segmentation model; keep both out
    # of the temp directory so a concurrent render cannot evict them.
    "TMPDIR": str(PIPER_CACHE_DIR),
}


# ── Image routing ───────────────────────────────────────────────────────────────
FLUX_UNET = "flux1-schnell-fp8.safetensors"
FLUX_CLIP = "clip_l.safetensors"
FLUX_T5 = "t5xxl_fp8_e4m3fn.safetensors"
FLUX_VAE = "ae.safetensors"
SDXL_CHECKPOINT = "sdxl_base_1.0.safetensors"
DEFAULT_IMAGE_MODEL = os.environ.get("BEEFTV_IMAGE_MODEL", "flux-schnell")


# ── Env helpers ─────────────────────────────────────────────────────────────────
# Acceleration switches are read per render, not at import time, so the process
# does not need a restart to compare variants. A malformed value falls back to
# the default instead of raising: a typo in an opt-in switch must never take the
# whole video endpoint down.
def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ[name])
    except (KeyError, TypeError, ValueError):
        return default


def _env_flag(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes", "on")


# Which H3 DiT checkpoint to load.
#
# fp8 is preferred because it measured 1.113x faster than int8 on the DGX
# Spark (scripts/bench_h3_variants.py). But this proxy runs on the Mac while the
# weights live on the DGX, so it cannot stat them, and no ComfyUI HTTP endpoint
# on this build distinguishes "absent" from "not enumerable":
#
#   * GET  /object_info?category=UNETLoader  -> empty options list
#   * POST /prompt with a bare UNETLoader    -> "Prompt has no outputs",
#     identically for a real checkpoint and for a made-up one
#
# Guessing wrong is expensive: a missing checkpoint fails later inside
# UNETLoader and turns every render into a confusing load error. So the choice
# is not probed, it is declared by whoever owns the files -- set
# BEEFTV_H3_UNET, and deploy/scripts/ does this from the DGX side where the
# files are actually visible. Absent that, stay on the known-good int8 build.
H3_UNET_INT8 = "minimax_h3_fl2va_pruned_int8_convrot.safetensors"
H3_UNET_FP8 = "minimax_h3_fl2va_pruned_fp8_scaled.safetensors"


# ── FastAPI App ───────────────────────────────────────────────────────────────
app = FastAPI(title="BeefTV ComfyUI Proxy", version="1.0.0")


@app.exception_handler(RequestValidationError)
async def log_request_validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
    """Log which field was rejected before the 422 reaches the client.

    A bare 422 with no body is impossible to act on: the caller cannot tell
    whether the payload was malformed or simply carried a field the request
    model does not know yet. The machine-readable detail is still returned
    unchanged; this only adds the request path and the rejected field names
    to the log.
    """
    rejected = sorted({str(item.get("loc", ["?"])[-1]) for item in exc.errors()})
    logging.warning(
        "request validation failed path=%s fields=%s",
        request.url.path,
        ",".join(rejected),
    )
    return JSONResponse(status_code=422, content={"detail": exc.errors()})

# ── Auth + CORS Middleware ─────────────────────────────────────────────────────

@app.middleware("http")
async def auth_and_cors_middleware(request: Request, call_next):
    """Gate /v1/* with token auth and tighten CORS origins.

    • Token auth: if BEEFTV_PROXY_TOKEN is set, every /v1/* request must carry
      X-Beeftv-Token: <token>. Requests without the header (or with a wrong
      value) get 401. /health and /view bypass token auth so the health-check
      and media-serving endpoints are still reachable.

    • CORS: restrict Access-Control-Allow-Origin to the explicit list configured
      in ALLOWED_ORIGINS (or localhost in dev). This replaces the previous
      allow_origins=["*"] which leaked media URLs to any cross-origin page.
    """
    path = request.url.path

    # ── Token auth for /v1/* ──
    # Accept either:
    #   • X-Beeftv-Token: <token>       (simple header, browser-friendly)
    #   • Authorization: Bearer <token> (standard OAuth 2.0 form; used by Go backend)
    if path.startswith("/v1/") and PROXY_TOKEN:
        provided = (
            request.headers.get("x-beeftv-token", "").strip()
            or request.headers.get("authorization", "").removeprefix("Bearer ").strip()
        )
        if not provided or not secrets.compare_digest(provided, PROXY_TOKEN):
            log.warning(f"Unauthorized /v1/* access from {request.client.host}")
            return JSONResponse(
                status_code=401,
                content={"detail": "Unauthorized — missing or invalid auth token"},
            )

    # ── CORS: handle preflight and inject headers on every response ──
    origin = request.headers.get("origin", "")
    response: Response = await call_next(request)

    if origin in ALLOWED_ORIGINS:
        response.headers["Access-Control-Allow-Origin"] = origin
        response.headers["Access-Control-Allow-Credentials"] = "true"
        response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
        response.headers["Access-Control-Allow-Headers"] = (
            "Content-Type, Authorization, X-Beeftv-Token"
        )

    # Block framing to prevent clickjacking on the media-view endpoint.
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response

# ── Request Models ────────────────────────────────────────────────────────────


class Message(BaseModel):
    role: str
    # Multimodal turns send ``content`` as a list of parts instead of a plain
    # string. Accept both and keep the parts so the upstream model sees them.
    content: Union[str, List[Dict[str, Any]], None] = None
    # Assistant turns can carry tool calls, and tool results come back as
    # separate messages. Both are required for the agent host to run a turn.
    tool_calls: Optional[List[Dict[str, Any]]] = None
    tool_call_id: Optional[str] = None
    name: Optional[str] = None
    reasoning_content: Optional[str] = None


class ToolDefinition(BaseModel):
    """One OpenAI function-tool declaration."""

    type: str = "function"
    function: Dict[str, Any] = Field(default_factory=dict)


def openai_message_payload(message: Message) -> Dict[str, Any]:
    """Project one request message into the OpenAI chat wire shape.

    ``content`` may be a plain string or a list of multimodal parts. The list
    form is forwarded verbatim because the upstream model understands it;
    only the extra assistant/tool bookkeeping fields are dropped or renamed.
    """
    payload: Dict[str, Any] = {"role": message.role, "content": message.content}
    if message.tool_calls:
        payload["tool_calls"] = message.tool_calls
    if message.tool_call_id:
        payload["tool_call_id"] = message.tool_call_id
    if message.name:
        payload["name"] = message.name
    return payload


def tool_call_arguments(raw: Any) -> str:
    """Normalise tool-call arguments to the OpenAI wire shape.

    OpenAI defines ``function.arguments`` as a **string** holding JSON.
    Ollama returns a decoded object instead, and some clients call
    ``JSON.parse`` on the value unconditionally — a dict there raises and
    the whole turn dies before the tool ever runs. Serialising a dict back
    to JSON keeps the contract; a string is passed through unchanged.
    """
    if isinstance(raw, str):
        return raw
    if raw is None:
        return "{}"
    try:
        return json.dumps(raw, ensure_ascii=False)
    except (TypeError, ValueError):
        return "{}"


def message_text(message: Message) -> str:
    """Flatten a request message into plain text.

    Multimodal turns send ``content`` as a list of parts; the prompt-length
    heuristics and the video prompt extraction only understand text.
    """
    content = message.content
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts: List[str] = []
    for part in content:
        if not isinstance(part, dict):
            continue
        if part.get("type") in ("text", "input_text"):
            parts.append(str(part.get("text", "")))
    return "\n".join(parts)


class ChatCompletionRequest(BaseModel):
    # Reject unknown fields rather than silently ignoring them — a typo like
    # ``temprature: 0.2`` would otherwise be swallowed and the request would
    # run with the default ``temperature=1.0``, which the caller never asked for.
    model_config = ConfigDict(extra="forbid")

    model: str = "minimax-h3"
    messages: List[Message] = Field(default_factory=list)
    max_tokens: Optional[int] = Field(default=None, ge=1, le=32768)
    # The agent SDK prefers the newer spelling. Both are accepted and
    # forwarded; the upstream model only understands ``max_tokens``.
    max_completion_tokens: Optional[int] = Field(default=None, ge=1, le=32768)
    temperature: float = Field(default=1.0, ge=0.0, le=2.0)
    stream: bool = False
    # Standard OpenAI chat fields. The agent host (pi SDK) always sends
    # ``tools``/``tool_choice``; rejecting them made every assistant turn
    # fail with 422 before the request ever reached the upstream model.
    tools: Optional[List[ToolDefinition]] = None
    tool_choice: Optional[Union[str, Dict[str, Any]]] = None
    top_p: Optional[float] = Field(default=None, ge=0.0, le=1.0)
    stop: Optional[Union[str, List[str]]] = None
    presence_penalty: Optional[float] = Field(default=None, ge=-2.0, le=2.0)
    frequency_penalty: Optional[float] = Field(default=None, ge=-2.0, le=2.0)
    n: Optional[int] = Field(default=None, ge=1)
    response_format: Optional[Dict[str, Any]] = None
    # OpenAI Responses-style switches. They carry no meaning for a local
    # OpenAI-compatible backend, but the SDK always sends them, so rejecting
    # them broke every assistant turn.
    store: Optional[bool] = None
    stream_options: Optional[Dict[str, Any]] = None
    width: int = Field(default=1344, ge=64, le=4096)
    height: int = Field(default=768, ge=64, le=4096)
    duration: int = Field(default=124, ge=17, le=409)
    seed: int = Field(default=0, ge=0)
    negative_prompt: str = ""
    steps: int = Field(default=4, ge=1, le=200)
    cfg: float = Field(default=1.0, ge=0.0, le=20.0)
    turbo: str = "4step"
    shift_video: float = Field(default=12.0, ge=0.0, le=20.0)
    shift_audio: float = Field(default=3.0, ge=0.0, le=20.0)


class ImageEditPart(BaseModel):
    """One entry of a multipart image-upload field.

    ``url`` accepts an http(s) URL, a ``data:`` URI, or a bare ComfyUI output
    filename so a caller can round-trip a file it just generated.
    """

    url: str


class ImageRequest(BaseModel):
    model: str = DEFAULT_IMAGE_MODEL
    prompt: str = ""
    n: int = 1
    size: str = "1024x1024"
    response_format: str = "url"
    seed: Optional[int] = None
    steps: Optional[int] = None
    guidance: Optional[float] = None
    # img2img only. When set, the reference image is fed in as a starting latent
    # at this denoise strength (1.0 ignores the source image entirely).
    strength: Optional[float] = None
    image: Optional[str] = None
    image_urls: Optional[List[str]] = None


class ImageEditRequest(ImageRequest):
    image: Optional[str] = None


class SpeechRequest(BaseModel):
    model: str = "piper"
    input: str
    voice: Optional[str] = None
    speed: float = 1.0
    pitch: float = 0.0
    volume: float = 1.0
    # OpenAI returns audio/wav by default; mp3 goes through ffmpeg.
    response_format: str = "wav"



# ── ComfyUI Client ────────────────────────────────────────────────────────────


async def comfy_prompt(workflow: dict, client_id: str) -> str:
    """Submit a workflow to ComfyUI and return the prompt_id."""
    async with httpx.AsyncClient(timeout=300) as client:
        r = await client.post(
            f"{COMFY_HOST}/prompt",
            json={"prompt": workflow, "client_id": client_id},
        )
    if r.status_code >= 400:
        # ComfyUI reports graph problems in a `node_errors` block; forwarding
        # that verbatim is the difference between an actionable message and a
        # bare 400 that tells the caller nothing.
        try:
            detail = r.json()
        except ValueError:
            detail = {"raw": r.text[:500]}
        log.error(f"ComfyUI rejected the graph: {json.dumps(detail)[:800]}")
        raise HTTPException(
            status_code=502,
            detail=f"ComfyUI rejected the workflow: {json.dumps(detail)[:600]}",
        )
    return r.json()["prompt_id"]



async def comfy_wait_and_fetch(
    prompt_id: str, timeout: int = 600
) -> Dict[str, Any]:
    """Poll history until the job completes or times out."""
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.get(f"{COMFY_HOST}/history/{prompt_id}")
        r.raise_for_status()
        history = r.json()
        if prompt_id in history:
            return history[prompt_id]
        await asyncio.sleep(5)
    raise TimeoutError(f"ComfyUI job {prompt_id} timed out after {timeout}s")


async def comfy_queue() -> list:
    """Return current queue state."""
    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.get(f"{COMFY_HOST}/queue")
    r.raise_for_status()
    return r.json()


# ── MiniMax H3 Workflow Builder ─────────────────────────────────────────────


def build_minimax_h3_workflow(
    prompt: str,
    width: int = 1344,
    height: int = 768,
    length_frames: int = 124,
    seed: int = 0,
    steps: int = 4,
    cfg: float = 1.0,
    shift_video: float = 12.0,
    shift_audio: float = 3.0,
    turbo_mode: str = "4step",
    filename_prefix: str = "video/BeefTV",
) -> dict:
    """Build the MiniMax H3 T2V workflow JSON for ComfyUI.

    The MiniMaxH3ImageToVideo node has no ``negative_prompt`` input — that is
    a node-schema fact, not a workflow choice — so any caller-supplied
    negative prompt is logged and dropped here rather than injected into a
    graph the DGX would reject.
    """
    use_turbo = turbo_mode != "off"
    if turbo_mode == "4step":
        turbo_lora = "minimax_h3_fl2v_turbo_4step_v1.0_768p_comfyui_bf16.safetensors"
        actual_steps = 4
    elif turbo_mode == "8step":
        turbo_lora = "minimax_h3_fl2v_turbo_8step_v1.0_comfyui_bf16.safetensors"
        actual_steps = 8
    elif turbo_mode == "ref2v":
        # Reference-to-video 4-step LoRA is a separate trained checkpoint.
        turbo_lora = "minimax_h3_ref2v_turbo_4step_v0.1_comfyui_bf16.safetensors"
        actual_steps = 4
    else:
        # ``off`` (or anything unknown) — skip the LoRA loader entirely so the
        # graph does not ask ComfyUI to load an empty lora_name.
        turbo_lora = None
        actual_steps = max(1, min(steps or 4, 50))
    actual_cfg = 1.0 if use_turbo else cfg

    # Text encoder selection. The int8 ConvRot checkpoint is the known-good
    # default; the nvfp4 (AWQ-packed, F8_E4M3) variant trades a little encoder
    # fidelity for ~11 GB less resident weight, which matters because the DGX
    # Spark has one 121 GiB unified pool shared with the DeepSeek server.
    # Only switch this after an end-to-end render proves the graph still
    # produces real video — a silently-degraded encoder looks like a working
    # render with wrong conditioning.
    clip_name = os.environ.get(
        "BEEFTV_H3_CLIP", "qwen3vl_32b_minimax_h3_int8_convrot.safetensors"
    )
    unet_name = os.environ.get("BEEFTV_H3_UNET", H3_UNET_INT8)
    nodes: Dict[str, Any] = {
        "1": {
            "class_type": "UNETLoader",
            "inputs": {
                "unet_name": unet_name,
                "weight_dtype": "default",
            },
        },
        "2": {
            "class_type": "CLIPLoader",
            "inputs": {
                "clip_name": clip_name,
                "type": "minimax",
                "weight_dtype": "default",
            },
        },
        "3": {
            "class_type": "VAELoader",
            "inputs": {"vae_name": "minimax_h3_video_vae_fp16.safetensors"},
        },
        "4": {
            "class_type": "VAELoader",
            "inputs": {"vae_name": "minimax_h3_audio_vae_fp32.safetensors"},
        },
        "5": {
            "class_type": "MiniMaxH3SigmaShift",
            "inputs": {
                "model": ["1", 0],
                "shift_video": shift_video,
                "shift_audio": shift_audio,
            },
        },
    }

    if turbo_lora is not None:
        # Wire the LoRA between the sigma shift and the BasicGuider so its
        # weight modifications reach the sampler.
        nodes["6"] = {
            "class_type": "LoraLoaderModelOnly",
            "inputs": {
                "model": ["5", 0],
                "lora_name": turbo_lora,
                "strength_model": 1.0,
            },
        }
        model_for_guider = ["6", 0]
    else:
        # No LoRA: wire the sigma-shifted model straight to the BasicGuider
        # and pass cfg through. Sampling on the full base model benefits
        # from a non-zero cfg; turbo distills the model to ignore it.
        model_for_guider = ["5", 0]

    # ── Optional inference accelerations ────────────────────────────────────
    # Both are opt-in because each trades output fidelity or first-render
    # latency for throughput, and both are inserted on the *model* edge only,
    # so the conditioning and VAE paths are untouched.
    cache_threshold = _env_float("BEEFTV_H3_EASYCACHE_THRESHOLD", 0.0)
    if (
        cache_threshold > 0
        and not _env_flag("BEEFTV_H3_EASYCACHE_ALLOW_AUDIO_LOSS")
    ):
        # EasyCache reuses a previous step's residual when the measured change
        # rate stays under the threshold. It is a sampler-level node, so it has
        # to wrap the model before the guider/scheduler/sampler chain reads it.
        #
        # It decides which steps to skip from the VIDEO stream and then replays
        # the cached residual on AUDIO too, but H3 advances the two on different
        # schedules (audio velocity is scaled by d(sigma_a)/d(sigma_v)), so a
        # residual cached at one step is mis-scaled when replayed at the next.
        # Measured on this graph's audio branch: RMS ratio drops to ~0.50 on
        # music prompts and spectral centroid shifts up, i.e. the low end is
        # simply gone — while video SSIM still reads 0.95, so watching the output
        # hides it entirely (ComfyUI#15326).
        #
        # This graph always muxes audio into the output MP4 (nodes 15/16), so
        # silently enabling EasyCache would ship a video that looks fine and
        # sounds wrong. Refuse unless the caller explicitly acknowledges it.
        log.warning(
            "Ignoring BEEFTV_H3_EASYCACHE_THRESHOLD=%s: EasyCache corrupts the "
            "H3 audio branch (RMS ~0.50 on music prompts) while video quality "
            "stays near-identical, so the damage is invisible in review. Set "
            "BEEFTV_H3_EASYCACHE_ALLOW_AUDIO_LOSS=1 to force it anyway.",
            cache_threshold,
        )
        cache_threshold = 0.0

    if cache_threshold > 0:
        nodes["20"] = {
            "class_type": "EasyCache",
            "inputs": {
                "model": model_for_guider,
                "reuse_threshold": cache_threshold,
                "start_percent": _env_float("BEEFTV_H3_EASYCACHE_START", 0.15),
                "end_percent": _env_float("BEEFTV_H3_EASYCACHE_END", 0.95),
                "verbose": False,
            },
        }
        model_for_guider = ["20", 0]

    if _env_flag("BEEFTV_H3_TORCH_COMPILE"):
        # Inductor compiles the DiT block graph once and reuses it. The first
        # render pays a multi-minute compile, which is why it stays opt-in.
        nodes["21"] = {
            "class_type": "TorchCompileModel",
            "inputs": {"model": model_for_guider, "backend": "inductor"},
        }
        model_for_guider = ["21", 0]

    nodes["7"] = {
        "class_type": "MiniMaxH3ImageToVideo",
        "inputs": {
            "clip": ["2", 0],
            "vae": ["3", 0],
            "prompt": prompt,
            "width": width,
            "height": height,
            "length": length_frames,
        },
    }
    nodes["8"] = {"class_type": "PrimitiveInt", "inputs": {"value": seed}}
    nodes["9"] = {
        "class_type": "RandomNoise",
        "inputs": {"noise_seed": ["8", 0]},
    }
    nodes["10"] = {
        "class_type": "BasicGuider",
        "inputs": {"model": model_for_guider, "conditioning": ["7", 0]},
    }
    nodes["11"] = {
        "class_type": "KSamplerSelect",
        "inputs": {"sampler_name": "res_multistep"},
    }
    nodes["12"] = {
        "class_type": "BasicScheduler",
        "inputs": {
            "model": model_for_guider,
            "scheduler": "simple",
            "steps": actual_steps,
            "denoise": 1.0,
        },
    }
    nodes["13"] = {
        "class_type": "SamplerCustomAdvanced",
        "inputs": {
            "noise": ["9", 0],
            "guider": ["10", 0],
            "sampler": ["11", 0],
            "sigmas": ["12", 0],
            "latent_image": ["7", 1],
        },
    }
    nodes["14"] = {
        "class_type": "VAEDecode",
        "inputs": {"samples": ["13", 0], "vae": ["3", 0]},
    }
    nodes["15"] = {
        "class_type": "VAEDecodeAudio",
        "inputs": {"samples": ["13", 0], "vae": ["4", 0]},
    }
    nodes["16"] = {
        "class_type": "CreateVideo",
        "inputs": {"images": ["14", 0], "audio": ["15", 0], "fps": 24},
    }
    nodes["17"] = {
        "class_type": "SaveVideo",
        "inputs": {
            "video": ["16", 0],
            "filename_prefix": filename_prefix,
            "format": "auto",
            "codec": "auto",
        },
    }
    return nodes


# ── Output Resolution ───────────────────────────────────────────────────────
#
# MiniMax H3 `SaveVideo` reports its .mp4 under `images` (not `videos`), so both
# shapes have to be inspected. ComfyUI serves output files from `/view`; the
# `/comfy-view` path only exists inside the bundled web UI and 404s on the API.
#
# URLs are pointed back at this bridge rather than at the DGX host, so the
# browser can always load the media regardless of CORS or LAN routing.


def resolve_output(
    outputs: Dict[str, Any], base_url: str = PUBLIC_BASE
) -> Optional[str]:
    """Return a fetchable URL for the first video produced by a job."""
    for node_output in outputs.values():
        videos = node_output.get("videos", [])
        # SaveVideo reports the mp4 under `images`. Entries carrying an explicit
        # non-output type are intermediates, but a missing `type` is treated as
        # an output so nodes that omit the field are not silently dropped.
        images = [
            img
            for img in node_output.get("images", [])
            if img.get("type", "output") == "output"
        ]
        for asset in list(videos) + list(images):
            filename = asset.get("filename")
            if not filename:
                continue
            params = f"filename={quote(filename)}&type={asset.get('type', 'output')}"
            subfolder = asset.get("subfolder", "")
            if subfolder:
                params += f"&subfolder={quote(subfolder)}"
            return f"{base_url}/view?{params}"
    return None


# ── SSE Helpers ─────────────────────────────────────────────────────────────


async def _poll_comfy_progress(prompt_id: str) -> AsyncGenerator[Dict[str, Any], None]:
    """Watch a ComfyUI job and yield progress markers until it finishes.

    The proxy does not depend on the ``websockets`` package, so this polls
    ComfyUI's HTTP endpoints instead of forwarding its WebSocket. ``/queue``
    reports whether the job is running / pending / done, and ``/history``
    only carries an entry once the graph has finished executing — together
    they let the caller distinguish model-loading from sampler running from
    decode/encode. The 2-second cadence keeps the front-end responsive
    without drowning the DGX in requests.
    """
    last_status: Optional[str] = None
    started_at = asyncio.get_event_loop().time()
    while True:
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                queue_resp = await client.get(f"{COMFY_HOST}/queue")
                history_resp = await client.get(
                    f"{COMFY_HOST}/history/{prompt_id}"
                )
            queue = queue_resp.json() if queue_resp.status_code == 200 else {}
            history = (
                history_resp.json() if history_resp.status_code == 200 else {}
            )
        except Exception as exc:  # noqa: BLE001
            log.debug(f"Progress poll failed for {prompt_id}: {exc}")
            await asyncio.sleep(2)
            continue

        if prompt_id in history:
            entry = history[prompt_id]
            status = entry.get("status", {})
            err = status.get("messages") or []
            last_err = next(
                (
                    m
                    for m in err
                    if isinstance(m, list) and m and m[0] == "execution_error"
                ),
                None,
            )
            if last_err:
                yield {
                    "event": "error",
                    "detail": json.dumps(last_err[1])[:500],
                }
                return
            yield {
                "event": "done",
                "prompt_id": prompt_id,
                "outputs": entry.get("outputs", {}),
                "elapsed_s": round(asyncio.get_event_loop().time() - started_at, 1),
            }
            return

        # Job is still active. Distinguish queued vs running.
        running_ids = {item[1] for item in queue.get("queue_running", [])}
        pending_ids = {item[1] for item in queue.get("queue_pending", [])}
        if prompt_id in running_ids:
            status = "running"
            phase = "Generating frames"
        elif prompt_id in pending_ids:
            status = "pending"
            phase = "Waiting in queue"
        else:
            # Briefly invisible between submission and queue insert.
            status = "starting"
            phase = "Submitting to ComfyUI"

        if status != last_status:
            yield {
                "event": "progress",
                "prompt_id": prompt_id,
                "status": status,
                "phase": phase,
                "elapsed_s": round(asyncio.get_event_loop().time() - started_at, 1),
            }
            last_status = status
        else:
            # Heartbeat: keep the connection warm and the front-end spinner
            # moving even when ComfyUI has not changed state for a while.
            yield {
                "event": "heartbeat",
                "prompt_id": prompt_id,
                "status": status,
                "phase": phase,
                "elapsed_s": round(asyncio.get_event_loop().time() - started_at, 1),
            }
        await asyncio.sleep(2)


async def sse_stream(
    prompt_id: str,
    model: str,
    release_vram: bool = True,
) -> AsyncGenerator[str, None]:
    """Yield SSE-formatted events for a ComfyUI job.

    ``release_vram`` triggers ComfyUI's ``/free`` endpoint after a successful
    render so the next LLM hop (DeepSeek V4) can load into VRAM without
    bumping into cached diffusion weights. Failures skip the release to
    avoid flushing an in-flight checkpoint retry.
    """
    yield "event: queued\ndata: "
    yield json.dumps({"event": "queued", "prompt_id": prompt_id, "model": model})
    yield "\n\n"

    try:
        outputs: Dict[str, Any] = {}
        async for update in _poll_comfy_progress(prompt_id):
            if update["event"] in ("progress", "heartbeat"):
                yield "event: progress\ndata: "
                yield json.dumps(update)
                yield "\n\n"
                continue
            if update["event"] == "done":
                outputs = update.get("outputs", {})
                url = resolve_output(outputs)
                if not url:
                    log.error(
                        f"[{prompt_id}] No video found in ComfyUI outputs: "
                        f"{list(outputs.keys())}"
                    )
                    yield "event: error\ndata: "
                    yield json.dumps(
                        {
                            "event": "error",
                            "prompt_id": prompt_id,
                            "detail": "ComfyUI produced no video output. "
                                      "The model may have failed silently — "
                                      "check the ComfyUI log for the prompt_id.",
                        }
                    )
                    yield "\n\n"
                    return
                yield "event: done\ndata: "
                yield json.dumps(
                    {
                        "event": "done",
                        "prompt_id": prompt_id,
                        "model": model,
                        "url": url,
                        "elapsed_s": update.get("elapsed_s"),
                    }
                )
                yield "\n\n"
                break
            if update["event"] == "error":
                yield "event: error\ndata: "
                yield json.dumps(
                    {
                        "event": "error",
                        "prompt_id": prompt_id,
                        "detail": update.get("detail", "ComfyUI execution failed"),
                    }
                )
                yield "\n\n"
                return
        if release_vram and outputs:
            try:
                async with httpx.AsyncClient(timeout=30) as client:
                    await client.post(
                        f"{COMFY_HOST}/free",
                        json={"unload_models": True, "free_memory": True},
                    )
                log.info(f"[{prompt_id}] Released ComfyUI model cache")
            except Exception as exc:  # noqa: BLE001
                log.warning(
                    f"[{prompt_id}] Could not release ComfyUI cache: {exc}"
                )
    except Exception as e:  # noqa: BLE001
        log.error(f"SSE stream error for {prompt_id}: {e}")
        yield "event: error\ndata: "
        yield json.dumps({"event": "error", "prompt_id": prompt_id, "detail": str(e)})
        yield "\n\n"


# ── Image Helpers ──────────────────────────────────────────────────────────────

# ComfyUI needs both side of every latent: the spatial shape for generation and
# the model patchifier for img2img. Keeping the two node graphs side by side in
# one dict is the documented ComfyUI convention for a dual-branch workflow.
def parse_size(size: str) -> tuple[int, int]:
    """Parse an OpenAI ``"WIDTHxHEIGHT"`` size string.

    Raises ``ValueError`` on malformed input rather than silently defaulting
    to 1024² — the previous behaviour sent the request to ComfyUI with a
    fallback size that ignored the caller's intent.
    """
    match = re.match(r"\s*(\d+)\s*[xX*]\s*(\d+)", size or "")
    if not match:
        raise ValueError(f"Invalid size '{size}', expected WIDTHxHEIGHT")
    width, height = int(match.group(1)), int(match.group(2))
    if width < 64 or height < 64:
        raise ValueError(f"size {width}x{height} is below the 64px minimum")
    if width > 4096 or height > 4096:
        raise ValueError(f"size {width}x{height} exceeds the 4096px maximum")
    # FLUX wants a multiple of 16; SDXL wants a multiple of 8.
    return (max(64, width - width % 16), max(64, height - height % 16))


def build_flux_workflow(
    prompt: str,
    width: int,
    height: int,
    seed: int,
    steps: int,
    guidance: float,
    count: int,
    filename_prefix: str,
    init_image: Optional[str] = None,
    strength: float = 0.75,
) -> dict:
    """FLUX.1 Schnell graph. Schnell is a distilled guidance-free model: it is
    trained for 1-4 steps and expects guidance pinned at 0.0."""
    steps = max(1, min(steps or 4, 8))
    workflow: Dict[str, Any] = {
        "1": {
            "class_type": "UNETLoader",
            "inputs": {"unet_name": FLUX_UNET, "weight_dtype": "default"},
        },
        "2": {
            "class_type": "DualCLIPLoader",
            "inputs": {"clip_name1": FLUX_CLIP, "clip_name2": FLUX_T5, "type": "flux"},
        },
        "3": {"class_type": "VAELoader", "inputs": {"vae_name": FLUX_VAE}},
        "4": {
            "class_type": "CLIPTextEncode",
            "inputs": {"text": prompt, "clip": ["2", 0]},
        },
        "5": {"class_type": "EmptySD3LatentImage", "inputs": {"width": width, "height": height, "batch_size": count}},
        "6": {"class_type": "ModelSamplingFlux", "inputs": {"model": ["1", 0], "max_shift": 1.15, "base_shift": 0.5, "width": width, "height": height}},
        "7": {
            "class_type": "RandomNoise",
            "inputs": {"noise_seed": seed},
        },
        "8": {"class_type": "BasicGuider", "inputs": {"model": ["6", 0], "conditioning": ["4", 0]}},
        "9": {
            "class_type": "KSamplerSelect",
            "inputs": {"sampler_name": "euler"},
        },
        "10": {
            "class_type": "BasicScheduler",
            "inputs": {"model": ["6", 0], "scheduler": "simple", "steps": steps, "denoise": 1.0},
        },
        "11": {
            "class_type": "SamplerCustomAdvanced",
            "inputs": {
                "noise": ["7", 0],
                "guider": ["8", 0],
                "sampler": ["9", 0],
                "sigmas": ["10", 0],
                "latent_image": ["5", 0],
            },
        },
        "12": {"class_type": "VAEDecode", "inputs": {"samples": ["11", 0], "vae": ["3", 0]}},
        "13": {
            "class_type": "SaveImage",
            "inputs": {"images": ["12", 0], "filename_prefix": filename_prefix},
        },
    }

    if init_image:
        # Swap the empty latent for a VAE-encoded version of the reference, and
        # drop denoise so the sampler starts partway through the trajectory.
        workflow["14"] = {"class_type": "LoadImage", "inputs": {"image": init_image}}
        workflow["15"] = {"class_type": "ImageScale", "inputs": {"image": ["14", 0], "upscale_method": "lanczos", "width": width, "height": height, "crop": "center"}}
        workflow["16"] = {"class_type": "VAEEncode", "inputs": {"pixels": ["15", 0], "vae": ["3", 0]}}
        workflow["11"]["inputs"]["latent_image"] = ["16", 0]
        workflow["10"]["inputs"]["denoise"] = max(0.05, min(strength, 1.0))
    return workflow


def build_sdxl_workflow(
    prompt: str,
    width: int,
    height: int,
    seed: int,
    steps: int,
    guidance: float,
    count: int,
    filename_prefix: str,
    negative_prompt: str = "",
    init_image: Optional[str] = None,
    strength: float = 0.75,
) -> dict:
    """SDXL base graph, used for img2img and as the fallback when FLUX is absent.

    The DGX carries a KSampler override (from the sampler-select extensions)
    whose signature takes an explicit `model` and `denoise` rather than
    inferring them, so the graph passes both. txt2img is expressed as an
    img2img at denoise 1.0 to keep one code path.
    """
    steps = max(1, steps or 30)
    denoise = max(0.05, min(strength, 1.0)) if init_image else 1.0
    workflow: Dict[str, Any] = {
        "1": {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": SDXL_CHECKPOINT}},
        "2": {"class_type": "CLIPTextEncode", "inputs": {"text": prompt, "clip": ["1", 1]}},
        "3": {
            "class_type": "CLIPTextEncode",
            "inputs": {"text": negative_prompt, "clip": ["1", 1]},
        },
        "4": {
            "class_type": "EmptyLatentImage",
            "inputs": {"width": width, "height": height, "batch_size": count},
        },
        "5": {
            "class_type": "KSampler",
            "inputs": {
                "model": ["1", 0],
                "seed": seed,
                "steps": steps,
                "cfg": guidance if guidance is not None else 7.0,
                "sampler_name": "dpmpp_2m",
                "scheduler": "karras",
                "denoise": denoise,
                "positive": ["2", 0],
                "negative": ["3", 0],
                "latent_image": ["4", 0],
            },
        },
        "6": {"class_type": "VAEDecode", "inputs": {"samples": ["5", 0], "vae": ["1", 2]}},
        "7": {
            "class_type": "SaveImage",
            "inputs": {"images": ["6", 0], "filename_prefix": filename_prefix},
        },
    }
    if init_image:
        # Feed the reference in as the starting latent, scaled to the requested
        # output size so the sampler never sees a shape mismatch.
        workflow["8"] = {"class_type": "LoadImage", "inputs": {"image": init_image}}
        workflow["9"] = {
            "class_type": "ImageScale",
            "inputs": {
                "image": ["8", 0],
                "upscale_method": "lanczos",
                "width": width,
                "height": height,
                "crop": "center",
            },
        }
        workflow["10"] = {"class_type": "VAEEncode", "inputs": {"pixels": ["9", 0], "vae": ["1", 2]}}
        workflow["5"]["inputs"]["latent_image"] = ["10", 0]
    return workflow




def comfy_available_models() -> Dict[str, List[str]]:
    """Read the live model lists ComfyUI advertises, so routing never points at
    a file that was never downloaded."""
    try:
        with httpx.Client(timeout=10) as client:
            resp = client.get(f"{COMFY_HOST}/object_info")
            resp.raise_for_status()
            info = resp.json()
    except (httpx.HTTPError, json.JSONDecodeError) as exc:
        log.warning(f"Could not read ComfyUI model catalogue: {exc}")
        return {}
    except Exception as exc:  # noqa: BLE001
        log.warning(f"Unexpected error reading ComfyUI model catalogue: {exc}")
        return {}

    def options(node: str, field: str) -> List[str]:
        try:
            return list(info[node]["input"]["required"][field][0])
        except (KeyError, IndexError, TypeError):
            return []

    return {
        "unet": options("UNETLoader", "unet_name"),
        "clip": options("DualCLIPLoader", "clip_name1"),
        "vae": options("VAELoader", "vae_name"),
        "ckpt": options("CheckpointLoaderSimple", "ckpt_name"),
    }


@functools.lru_cache(maxsize=1)
def _comfy_available_models_cached(_stamp: float) -> Dict[str, List[str]]:
    """LRU-cached wrapper around ``comfy_available_models`` with a 60s TTL.

    Without this, every image request paid the full ``/object_info`` round-trip
    and blocked the event loop on a synchronous ``urllib.request.urlopen``.
    """
    return comfy_available_models()


async def comfy_available_models_async() -> Dict[str, List[str]]:
    """Async, cache-aware lookup. Use from async route handlers."""
    stamp = time.time() // 60  # 60-second cache window
    return await asyncio.to_thread(_comfy_available_models_cached, stamp)


def flux_is_ready() -> bool:
    """FLUX only counts as available when the UNET, both encoders and the VAE
    are all present — a partial install produces a confusing load error deep
    inside a render, which is much worse than a clean fallback to SDXL."""
    available = comfy_available_models()
    if not available:
        return False
    return (
        FLUX_UNET in available.get("unet", [])
        and FLUX_CLIP in available.get("clip", [])
        and FLUX_T5 in available.get("clip", [])
        and FLUX_VAE in available.get("vae", [])
    )


async def flux_is_ready_async() -> bool:
    """Async variant of ``flux_is_ready`` that hits ComfyUI in a thread.

    The sync version called ``urllib.request.urlopen`` from inside an async
    route handler — that blocked the entire uvicorn worker for the duration of
    the HTTP round-trip, which under load serialised every in-flight request.
    """
    available = await comfy_available_models_async()
    if not available:
        return False
    return (
        FLUX_UNET in available.get("unet", [])
        and FLUX_CLIP in available.get("clip", [])
        and FLUX_T5 in available.get("clip", [])
        and FLUX_VAE in available.get("vae", [])
    )


async def build_image_workflow_async(
    prompt: str,
    model: str,
    width: int,
    height: int,
    seed: Optional[int],
    steps: Optional[int],
    guidance: Optional[float],
    count: int,
    init_image: Optional[str] = None,
    strength: Optional[float] = None,
) -> tuple[dict, str]:
    """Async variant of ``build_image_workflow`` — use from async route handlers.

    Picks the image backend and builds its graph. The previous sync version
    blocked the event loop on ``urllib``; this version off-loads the model
    catalogue probe to a thread.
    """
    count = max(1, min(count or 1, 4))
    resolved_seed = seed if seed is not None else int.from_bytes(os.urandom(4), "big")
    resolved_strength = strength if strength is not None else 0.75
    model_id = (model or DEFAULT_IMAGE_MODEL).lower()
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    mode = "img2img" if init_image else "txt2img"
    prefix = f"image/BeefTV-{mode}-{stamp}"

    wants_flux = "flux" in model_id
    wants_sdxl = "sdxl" in model_id

    flux_ready = await flux_is_ready_async()

    if wants_flux and not flux_ready:
        log.warning("FLUX requested but incomplete on the DGX — falling back to SDXL")
    use_flux = wants_flux or (flux_ready and not wants_sdxl)

    if use_flux:
        workflow = build_flux_workflow(
            prompt=prompt,
            width=width,
            height=height,
            seed=resolved_seed,
            steps=steps or 4,
            guidance=0.0,
            count=count,
            filename_prefix=prefix,
            init_image=init_image,
            strength=resolved_strength,
        )
    else:
        workflow = build_sdxl_workflow(
            prompt=prompt,
            width=width,
            height=height,
            seed=resolved_seed,
            steps=steps or 30,
            guidance=guidance if guidance is not None else 7.0,
            count=count,
            filename_prefix=prefix,
            init_image=init_image,
            strength=resolved_strength,
        )
    return workflow, prefix


def build_image_workflow(
    prompt: str,
    model: str,
    width: int,
    height: int,
    seed: Optional[int],
    steps: Optional[int],
    guidance: Optional[float],
    count: int,
    init_image: Optional[str] = None,
    strength: Optional[float] = None,
) -> tuple[dict, str]:
    """Sync shim — kept for tests and non-async callers.

    Calls ``asyncio.run`` only when there is no running event loop, otherwise
    routes through ``asyncio.to_thread`` so the urllib probe does not block
    a worker that is already inside an event loop.
    """
    try:
        asyncio.get_running_loop()
        # We're in an event loop — defer to the async variant.
        raise RuntimeError(
            "build_image_workflow called from inside a running event loop; "
            "use build_image_workflow_async instead"
        )
    except RuntimeError as exc:
        if "no running event loop" in str(exc):
            return asyncio.run(
                build_image_workflow_async(
                    prompt=prompt,
                    model=model,
                    width=width,
                    height=height,
                    seed=seed,
                    steps=steps,
                    guidance=guidance,
                    count=count,
                    init_image=init_image,
                    strength=strength,
                )
            )
        raise


async def stage_reference_image(source: str, client: httpx.AsyncClient) -> str:
    """Upload a reference image into ComfyUI's input folder and return its name.

    Accepts a bare ComfyUI output filename, a data: URI, or an http(s) URL, so a
    caller can feed back a file this very bridge just produced.

    A bare filename is ambiguous: ComfyUI only lets ``LoadImage`` read from its
    input folder, but freshly generated files live in the output folder. Rather
    than reject the round-trip, the file is re-fetched from ``/view`` and
    re-uploaded into input, which is the only way to feed a previous generation
    back into a new graph.
    """
    name = source.split("?", 1)[0].rsplit("/", 1)[-1]

    if source.startswith("data:"):
        header, _, payload = source.partition(",")
        if not payload:
            raise HTTPException(status_code=400, detail="Malformed data URI")
        ext = "png"
        if "image/" in header and "/" in header.split("image/", 1)[1]:
            ext = header.split("image/", 1)[1].split(";")[0] or "png"
        blob = base64.b64decode(payload)
    elif source.startswith(("http://", "https://")):
        resp = await client.get(source, timeout=120)
        resp.raise_for_status()
        blob = resp.content
    else:
        # Bare filename. If ComfyUI already has it in input, reuse it as-is.
        probe = await client.get(
            f"{COMFY_HOST}/view",
            params={"filename": name, "type": "input"},
        )
        if probe.status_code == 200:
            return name
        # Otherwise it is an output file (or a path served through this
        # bridge) — pull the bytes down and re-upload them into input.
        for params in (
            {"filename": name, "type": "output"},
            {"filename": name, "type": "output", "subfolder": "image"},
        ):
            resp = await client.get(f"{COMFY_HOST}/view", params=params)
            # Status check matters here: a 404 from ComfyUI can come back with
            # a non-empty HTML error body, and the previous truthiness-only
            # check would happily upload that HTML as if it were the image.
            if resp.status_code == 200 and resp.content:
                blob = resp.content
                break
        else:
            raise HTTPException(
                status_code=400,
                detail=f"Reference image '{name}' not found in ComfyUI input or output",
            )

    if not name or "." not in name:
        name = f"ref-{uuid.uuid4().hex[:8]}.png"

    resp = await client.post(
        f"{COMFY_HOST}/upload/image",
        files={"image": (name, blob, "application/octet-stream")},
        data={"overwrite": "true", "type": "input"},
        timeout=300,
    )
    if resp.status_code >= 400:
        raise HTTPException(
            status_code=502, detail=f"ComfyUI rejected the upload: {resp.text[:300]}"
        )
    uploaded = resp.json()
    return uploaded.get("name", name)



def collect_images(outputs: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Pull every produced still out of a ComfyUI history entry."""
    found: List[Dict[str, Any]] = []
    for node_output in outputs.values():
        images = [
            img
            for img in node_output.get("images", [])
            if img.get("type", "output") == "output"
        ]
        found.extend(images)
    return found


async def shape_images(
    images: List[Dict[str, Any]],
    response_format: str,
    base_url: str,
) -> Dict[str, Any]:
    """Convert ComfyUI image descriptors into an OpenAI images response."""
    data = []
    for asset in images:
        filename = asset.get("filename")
        if not filename:
            continue
        params = f"filename={quote(filename)}&type=output"
        subfolder = asset.get("subfolder", "")
        if subfolder:
            params += f"&subfolder={quote(subfolder)}"
        url = f"{base_url}/view?{params}"
        if response_format == "b64_json":
            # b64_json must carry the bytes themselves — a URL here would hand
            # the caller a link where it asked for inline data.
            try:
                blob = await fetch_comfy_file(params)
            except Exception as exc:  # noqa: BLE001
                log.error(f"Could not inline {filename}: {exc}")
                continue
            item: Dict[str, Any] = {
                "b64_json": base64.b64encode(blob).decode("ascii")
            }
        else:
            item = {"url": url}
        data.append(item)

    return {"created": int(datetime.now().timestamp()), "data": data}


async def fetch_comfy_file(params: str) -> bytes:
    """Read a ComfyUI output file straight into memory.

    Kept async on purpose: callers live inside an asyncio event loop, and the
    previous synchronous ``urllib.request.urlopen`` blocked every other
    in-flight request on the worker until the bytes were read. The smoke test
    under 5 concurrent ``b64_json`` image requests used to serialise the entire
    proxy behind a single image's download.
    """
    async with httpx.AsyncClient(timeout=300) as client:
        resp = await client.get(f"{COMFY_HOST}/view?{params}")
        resp.raise_for_status()
        return resp.content



async def run_image_workflow(workflow: dict, prefix: str) -> List[Dict[str, Any]]:
    """Submit an image graph and block until every image is written.

    Releases ComfyUI's cached weights on the way out so the next LLM call
    (DeepSeek V4 on the same DGX) does not stall waiting for VRAM. Skipped
    on the failure path so a partial render's model is still around for an
    immediate retry.
    """
    client_id = str(uuid.uuid4())
    prompt_id = await comfy_prompt(workflow, client_id)
    log.info(f"[{prompt_id}] Submitted image job ({prefix})")
    result = await comfy_wait_and_fetch(prompt_id, timeout=600)
    images = collect_images(result.get("outputs", {}))
    if not images:
        raise HTTPException(status_code=502, detail="ComfyUI produced no image")
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            await client.post(
                f"{COMFY_HOST}/free",
                json={"unload_models": True, "free_memory": True},
            )
        log.info(f"[{prompt_id}] Released ComfyUI model cache after image job")
    except Exception as exc:  # noqa: BLE001
        log.warning(f"[{prompt_id}] Could not release ComfyUI cache: {exc}")
    return images


# ── Speech Helpers ─────────────────────────────────────────────────────────────


def list_piper_voices() -> List[str]:
    """Discover installed Piper voices by their .onnx.json sidecar."""
    if not PIPER_VOICE_DIR.is_dir():
        return []
    return sorted(path.name[: -len(".onnx.json")] for path in PIPER_VOICE_DIR.glob("*.onnx.json"))


def resolve_voice(voice: Optional[str]) -> str:
    """Map a requested voice onto an installed one, tolerating partial names."""
    available = list_piper_voices()
    if not available:
        raise FileNotFoundError(f"No Piper voices found in {PIPER_VOICE_DIR}")
    if not voice or voice == "alloy" or voice == "auto":
        return DEFAULT_VOICE if DEFAULT_VOICE in available else available[0]
    if voice in available:
        return voice
    # Allow "huayan" or "zh_CN-huayan-medium" to both resolve.
    stem = voice.removesuffix("-medium").removesuffix(".onnx")
    for candidate in available:
        if stem and stem in candidate:
            return candidate
    log.warning(f"Voice '{voice}' not installed; using {DEFAULT_VOICE}")
    return DEFAULT_VOICE if DEFAULT_VOICE in available else available[0]


async def piper_synthesize(req: SpeechRequest) -> tuple[bytes, str]:
    """Run Piper and return (audio bytes, media type)."""
    if not Path(PIPER_BIN).is_file():
        raise FileNotFoundError(f"Piper binary missing at {PIPER_BIN}")

    voice = resolve_voice(req.voice)
    model_path = PIPER_VOICE_DIR / f"{voice}.onnx"
    config_path = PIPER_VOICE_DIR / f"{voice}.onnx.json"
    if not model_path.is_file():
        raise FileNotFoundError(f"Piper voice file missing: {model_path}")

    out_fmt = (req.response_format or "wav").lower()
    PIPER_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    workdir = Path(tempfile.mkdtemp(prefix="beeftv-tts-", dir=str(PIPER_CACHE_DIR)))
    wav_path = workdir / "out.wav"
    try:
        # Piper has no native pitch control; `length_scale` is the inverse of
        # speed, so >1 slows the delivery down.
        length_scale = 1.0 / max(0.1, req.speed)
        cmd = [
            str(PIPER_BIN),
            "--model", str(model_path),
            "--config", str(config_path),
            "--output_file", str(wav_path),
            "--length_scale", f"{length_scale:.3f}",
        ]
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(PIPER_CACHE_DIR),
            env=PIPER_ENV,
        )
        _, stderr = await proc.communicate(req.input.encode("utf-8"))
        if proc.returncode != 0 or not wav_path.is_file():
            raise RuntimeError(f"Piper failed: {stderr.decode('utf-8', 'replace')[:400]}")

        audio = wav_path.read_bytes()
        if not audio:
            raise RuntimeError("Piper produced an empty file")
        if out_fmt == "wav":
            return audio, "audio/wav"
        if out_fmt in ("mp3", "mpeg"):
            converted = await ffmpeg_convert(wav_path, out_fmt, req.volume, workdir)
            return converted, f"audio/{out_fmt}"
        # ogg/opus/aac and anything else we do not have an encoder for still
        # return wav, which every browser plays.
        return audio, "audio/wav"
    finally:
        shutil.rmtree(workdir, ignore_errors=True)



async def ffmpeg_convert(src: Path, fmt: str, volume: float, workdir: Path) -> bytes:
    """Re-encode Piper's wav output, applying a volume gain on the way."""
    if shutil.which(FFMPEG_BIN) is None:
        raise RuntimeError(f"{FFMPEG_BIN} is not installed; cannot produce {fmt}")
    dest = workdir / f"out.{fmt}"
    cmd = [FFMPEG_BIN, "-y", "-i", str(src), "-af", f"volume={max(0.0, volume):.2f}", str(dest)]
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE
    )
    _, stderr = await proc.communicate()
    if proc.returncode != 0 or not dest.is_file():
        raise RuntimeError(f"ffmpeg failed: {stderr.decode('utf-8', 'replace')[:400]}")
    return dest.read_bytes()


# ── Text Routing ───────────────────────────────────────────────────────────────


# Matches all emoji and miscellaneous decorative Unicode characters that should
# never surface in user-facing UI text. Covers:
#   - Emoji (U+1F300–U+1F9FF, supplemental planes)
#   - Dingbats, symbols, transport, misc pictographs, flags, tag sequences
#   - Variation selectors that attach to emoji
# Intentionally leaves CJK, standard punctuation, Latin/Greek/Cyrillic, ASCII untouched.
_STRIP_EMOJI_RE = re.compile(
    "["
    "\U0001F000-\U0001F9FF"  # emoticons, pictographs, symbols, arrows, transport, flags
    "\U0001FA00-\U0001FAFF"  # chess, zodiac, playing-card symbols
    "\U0001FB00-\U0001FBFF"  # enclosed alphanumerics supplement
    "\U0001FC00-\U0001FCFF"  # enclosed ideographic supplement
    "\U0001FD00-\U0001FDFF"  # symbols and pictographs supplement
    "\U0001FE00-\U0001FEFF"  # enclosed alphanumerics / transport / symbols
    "\U0001FF00-\U0001FFFF"  # enclosed ideographic supplement (high)
    "\u2600-\u26FF"          # misc symbols
    "\u2700-\u27BF"          # dingbats
    "\u2B50\u2B55"           # star, heavy circle
    "\u3030\u303D"           # wavy dash, part alternation mark
    "\u3297\u3299"           # circled ideograph
    "\uFE0F\uFE30"           # variation selectors
    "\U000E0020-\U000E007F"  # tag characters (flags, emoji skin tones)
    "]"
)


def _strip_emoji(text: str) -> str:
    """Return text with every decorative Unicode character removed.
    Applied to every LLM output before it leaves the proxy.
    """
    return _STRIP_EMOJI_RE.sub("", text)


# Qwen3 thinking-mode models write the user-visible answer into
# ``message.thinking`` (or a ``<think>...</think>`` block at the head of
# ``message.content``) and leave ``message.content`` empty. Shipping that
# empty string back to an OpenAI-shaped client looked like a silent 200-OK
# failure, so we now strip the think block and fall back to the thinking
# field when needed.
_THINK_BLOCK_RE = re.compile(r"<think>.*?</think>", re.DOTALL)


def extract_text_from_ollama_response(data: Dict[str, Any]) -> str:
    """Pull the user-visible answer out of an Ollama ``/api/chat`` response.

    Order of preference:
    1. ``message.content`` after stripping any leading ``<think>`` block.
    2. ``message.thinking`` after the same strip, with the obvious
       monologue pattern (the model repeating a phrase while it deliberates)
       trimmed from the tail.
    3. Empty string — caller decides how to surface this.
    """
    message = data.get("message") or {}
    content = (message.get("content") or "").strip()
    thinking = (message.get("thinking") or "").strip()

    if content:
        content = _THINK_BLOCK_RE.sub("", content).strip()
        if content:
            return content

    if not thinking:
        return ""

    thinking = _THINK_BLOCK_RE.sub("", thinking).strip()
    if not thinking:
        return ""

    # The thinking trace often ends with the model deliberating about a
    # short phrase it is trying to count, e.g. "Hello (1), how (2), are (3),
    # you (4)? No, that's four words. Wait, ...". Look for the last
    # numbered-counting line ("Wait, ...", "No, ...", "Let me count ...")
    # and cut there. Failing that, cut at the last blank line and keep the
    # last paragraph.
    cut_markers = [
        r"\n\s*Wait[,，].*$",
        r"\n\s*No[,，].*$",
        r"\n\s*Let me (count|think|re-?count).*$",
        r"\n\s*Hmm[,.]?.*$",
        r"\n\s*Alternatively[,，]?.*$",
        r"\n\s*Maybe.*$",
    ]
    best = thinking
    for marker in cut_markers:
        m = re.search(marker, best, flags=re.DOTALL)
        if m:
            best = best[: m.start()].rstrip()
            break
    if best:
        return best

    return thinking


async def ollama_chat(req: ChatCompletionRequest) -> Dict[str, Any]:
    """Chat via the Mac Studio's local Ollama."""
    options: Dict[str, Any] = {}
    if req.temperature is not None:
        options["temperature"] = req.temperature
    # ``max_tokens`` was previously dropped on the Ollama path — the request hit
    # the model with no cap and the model ran until it hit its own stop tokens,
    # which the smoke test surfaced as 338 tokens returned for a "say hi in 5
    # words" request. Forward both fields unconditionally now.
    if req.max_tokens is not None:
        options["num_predict"] = req.max_tokens
    payload: Dict[str, Any] = {
        "model": OLLAMA_MODEL,
        "messages": [openai_message_payload(m) for m in req.messages],
        "stream": False,
    }
    if options:
        payload["options"] = options
    # Ollama accepts OpenAI-style tool declarations. Forward them so the
    # assistant host can drive canvas operations on the Ollama path too.
    if req.tools:
        payload["tools"] = [tool.model_dump(exclude_none=True) for tool in req.tools]
    # Qwen3 is a thinking-mode model. By default it spends most of its
    # output budget deliberating in ``message.thinking`` and leaves
    # ``message.content`` empty until it is happy with the answer — for
    # short prompts the user only sees a stream of "let me count the
    # words ..." with no final response. ``think: false`` (Ollama ≥ 0.5)
    # skips the deliberation phase entirely and forces the model to
    # answer directly in ``content``. The OpenAI-shaped client then sees
    # a real answer instead of an empty bubble.
    payload["think"] = False
    async with httpx.AsyncClient(timeout=300) as client:
        resp = await client.post(f"{OLLAMA_URL}/api/chat", json=payload)
    resp.raise_for_status()
    data = resp.json()
    raw_content = extract_text_from_ollama_response(data)
    # Ollama returns tool calls in its own shape; the OpenAI-compatible client
    # expects ``choices[].message.tool_calls``. Normalise so the assistant host
    # can execute the operation and continue the turn.
    raw_tool_calls = data.get("message", {}).get("tool_calls") or []
    tool_calls = []
    for index, call in enumerate(raw_tool_calls):
        function = call.get("function", {}) if isinstance(call, dict) else {}
        tool_calls.append({
            "id": call.get("id", f"call_{index}") if isinstance(call, dict) else f"call_{index}",
            "type": "function",
            "function": {
                "name": function.get("name", ""),
                "arguments": tool_call_arguments(function.get("arguments")),
            },
        })
    message: Dict[str, Any] = {
        "role": "assistant",
        "content": raw_content if raw_content else "",
    }
    if tool_calls:
        message["tool_calls"] = tool_calls
    elif not raw_content:
        message["content"] = "（模型只返回了思考过程，没有可显示的答案。）"
    return {
        "id": f"chatcmpl-{uuid.uuid4().hex[:8]}",
        "object": "chat.completion",
        "created": int(datetime.now().timestamp()),
        "model": req.model,
        "choices": [
            {
                "index": 0,
                "message": message,
                "finish_reason": "tool_calls" if tool_calls else "stop",
            }
        ],
        "usage": {
            "prompt_tokens": data.get("prompt_eval_count", 0),
            "completion_tokens": data.get("eval_count", 0),
            "total_tokens": data.get("prompt_eval_count", 0) + data.get("eval_count", 0),
        },
        # ``x-beeftv-backend`` is set on the response header by the route
        # handler, not on the body — OpenAI-compatible clients (LiteLLM,
        # LangChain) reject unknown top-level body keys.
    }


async def ollama_chat_with_label(req: ChatCompletionRequest) -> tuple[Dict[str, Any], str]:
    """Run ``ollama_chat`` and return ``(response, backend_label)``.

    The label is meant to be sent as the ``x-beeftv-backend`` response header
    so callers can tell which leg served a request without polluting the
    OpenAI-shaped response body.
    """
    return await ollama_chat(req), f"ollama/{OLLAMA_MODEL}"


async def _text_sse_stream(req, backend):
    """Yield OpenAI chat-completion SSE chunks for text models.

    The previous implementation only streamed for video models, and any text
    request with stream=true silently fell back to a buffered response. This
    generator bridges Ollama (NDJSON) into the single OpenAI SSE shape that
    the BeefTV web client expects.
    """
    completion_id = f"chatcmpl-{uuid.uuid4().hex[:8]}"
    created = int(datetime.now().timestamp())
    role_chunk = {
        "id": completion_id,
        "object": "chat.completion.chunk",
        "created": created,
        "model": req.model,
        "choices": [
            {"index": 0, "delta": {"role": "assistant", "content": ""}, "finish_reason": None}
        ],
    }
    yield "data: " + json.dumps(role_chunk) + "\n\n"

    async def emit(content, finish=None):
        chunk = {
            "id": completion_id,
            "object": "chat.completion.chunk",
            "created": created,
            "model": req.model,
            "choices": [
                {"index": 0, "delta": {"content": content}, "finish_reason": finish}
            ],
        }
        yield "data: " + json.dumps(chunk) + "\n\n"

    async def emit_delta(delta, finish=None):
        """Forward an arbitrary delta (content, tool_calls, role)."""
        chunk = {
            "id": completion_id,
            "object": "chat.completion.chunk",
            "created": created,
            "model": req.model,
            "choices": [
                {"index": 0, "delta": delta, "finish_reason": finish}
            ],
        }
        yield "data: " + json.dumps(chunk) + "\n\n"

    payload = {
        "model": OLLAMA_MODEL,
        "messages": [openai_message_payload(m) for m in req.messages],
        "stream": True,
    }
    if req.temperature is not None:
        payload["options"] = {"temperature": req.temperature}
    if req.max_tokens:
        payload["options"] = {**payload.get("options", {}), "num_predict": req.max_tokens}
    # Tool declarations must be forwarded in streaming mode too, otherwise
    # the model answers the assistant host in prose instead of calling a
    # canvas operation.
    if req.tools:
        payload["tools"] = [tool.model_dump(exclude_none=True) for tool in req.tools]
    async with httpx.AsyncClient(timeout=OLLAMA_TIMEOUT) as client:
        async with client.stream("POST", f"{OLLAMA_URL}/api/chat", json=payload) as resp:
            resp.raise_for_status()
            tool_call_index = 0
            async for line in resp.aiter_lines():
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                content = obj.get("message", {}).get("content")
                if content:
                    async for piece in emit(content):
                        yield piece
                # Ollama streams tool calls as complete objects; convert them
                # into the OpenAI tool_calls delta shape.
                for call in obj.get("message", {}).get("tool_calls") or []:
                    function = call.get("function", {}) if isinstance(call, dict) else {}
                    delta = {
                        "tool_calls": [{
                            "index": tool_call_index,
                            "id": call.get("id", f"call_{tool_call_index}") if isinstance(call, dict) else f"call_{tool_call_index}",
                            "type": "function",
                            "function": {
                                "name": function.get("name", ""),
                                "arguments": tool_call_arguments(function.get("arguments")),
                            },
                        }],
                    }
                    tool_call_index += 1
                    async for piece in emit_delta(delta):
                        yield piece
                if obj.get("done"):
                    break
    async for piece in emit("", finish="stop"):
        yield piece
    yield "data: [DONE]\n\n"



# ── Routes ───────────────────────────────────────────────────────────────────


@app.get("/health")
async def health():
    """Report reachability of every backend, so a failure names the culprit
    instead of surfacing as an opaque 500 later."""
    backends: Dict[str, Any] = {}
    try:
        q = await comfy_queue()
        backends["dgx_comfyui"] = {
            "status": "ok",
            "url": COMFY_HOST,
            "queue_running": len(q.get("queue_running", [])),
            "queue_pending": len(q.get("queue_pending", [])),
        }
    except Exception as exc:  # noqa: BLE001
        backends["dgx_comfyui"] = {"status": "error", "url": COMFY_HOST, "detail": str(exc)}

    # A blocking read of the model catalogue is cheap but not free; only do it
    # when the caller actually asks.
    try:
        flux_ready = await asyncio.to_thread(flux_is_ready)
        backends["dgx_flux"] = {"status": "ok" if flux_ready else "missing"}
    except Exception as exc:  # noqa: BLE001
        backends["dgx_flux"] = {"status": "error", "detail": str(exc)}

    try:
        async with httpx.AsyncClient(timeout=5) as client:
            resp = await client.get(f"{OLLAMA_URL}/api/tags")
        tags = [m.get("name") for m in resp.json().get("models", [])]
        backends["mac_ollama"] = {
            "status": "ok" if OLLAMA_MODEL in tags else "model_missing",
            "url": OLLAMA_URL,
            "model": OLLAMA_MODEL,
        }
    except Exception as exc:  # noqa: BLE001
        backends["mac_ollama"] = {"status": "error", "url": OLLAMA_URL, "detail": str(exc)}

    voices = await asyncio.to_thread(list_piper_voices)
    backends["mac_piper"] = {
        "status": "ok" if voices else "no_voices",
        "bin": PIPER_BIN,
        "voices": voices,
    }

    degraded = [name for name, info in backends.items() if info.get("status") not in ("ok",)]
    return JSONResponse(
        {
            "status": "ok" if not degraded else "degraded",
            "degraded": degraded,
            "backends": backends,
        }
    )


def _model_entry(model_id: str, owned_by: str) -> Dict[str, Any]:
    return {
        "id": model_id,
        "object": "model",
        "created": 1700000000,
        "owned_by": owned_by,
        "permission": [],
        "root": model_id,
    }


@app.get("/v1/models")
async def list_models():
    """Return every routable model in OpenAI format."""
    return JSONResponse(
        {
            "object": "list",
            "data": [
                _model_entry("minimax-h3", "minimax"),
                _model_entry("wan-2.1-t2v", "stepfun"),
                _model_entry(FLUX_UNET.replace(".safetensors", "").replace("flux1-", "flux-"), "black-forest-labs"),
                _model_entry("sdxl-base", "stabilityai"),
                _model_entry(OLLAMA_MODEL, "ollama"),
                _model_entry("piper", "rhasspy"),
            ],
        }
    )



@app.get("/view")
async def view_output(request: Request):
    """
    Stream a generated file straight through from ComfyUI.

    The BeefTV web app sits on a different origin than the DGX ComfyUI, so the
    browser cannot always load DGX-hosted media directly. Serving the bytes from
    this bridge keeps a single reachable origin for the frontend.
    """
    params = {
        key: value
        for key, value in request.query_params.items()
        if key in ("filename", "subfolder", "type")
    }
    filename = params.get("filename") or ""
    if not filename:
        raise HTTPException(status_code=400, detail="filename is required")
    # Path-traversal guard: filenames that escape the ComfyUI input/output tree
    # (e.g. "../../etc/passwd" or "/etc/passwd") must be rejected here, before
    # the upstream ComfyUI call. The smoke test confirms ComfyUI itself returns
    # 404 for these, but we still block them explicitly so the bridge never
    # even attempts the lookup — defence in depth.
    if ".." in filename or filename.startswith(("/", "\\")) or any(ord(c) < 32 for c in filename):
        raise HTTPException(status_code=400, detail="Invalid filename")
    subfolder = params.get("subfolder") or ""
    if ".." in subfolder or subfolder.startswith(("/", "\\")):
        raise HTTPException(status_code=400, detail="Invalid subfolder")

    # Open the upstream stream first so a missing file still produces a proper
    # error response instead of a 200 whose body turns out to be a failure.
    client = httpx.AsyncClient(timeout=300)
    upstream = await client.send(
        client.build_request("GET", f"{COMFY_HOST}/view", params=params),
        stream=True,
    )
    if upstream.status_code != 200:
        await upstream.aclose()
        await client.aclose()
        raise HTTPException(
            status_code=upstream.status_code, detail="ComfyUI file not found"
        )

    async def body() -> AsyncGenerator[bytes, None]:
        try:
            async for chunk in upstream.aiter_bytes():
                yield chunk
        finally:
            await upstream.aclose()
            await client.aclose()

    return StreamingResponse(
        body(),
        media_type=upstream.headers.get("content-type", "application/octet-stream"),
        headers={
            "Content-Disposition": f'inline; filename="{params["filename"]}"',
        },
    )


@app.post("/v1/images/generations")
async def image_generations(req: ImageRequest):
    """Text-to-image. Defaults to FLUX.1 Schnell, falls back to SDXL."""
    if not req.prompt.strip():
        raise HTTPException(status_code=400, detail="prompt is required")
    try:
        width, height = parse_size(req.size)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    async with httpx.AsyncClient(timeout=300) as client:
        init_image = None
        if req.image:
            init_image = await stage_reference_image(req.image, client)
        workflow, prefix = await build_image_workflow_async(
            prompt=req.prompt,
            model=req.model,
            width=width,
            height=height,
            seed=req.seed,
            steps=req.steps,
            guidance=req.guidance,
            count=req.n,
            init_image=init_image,
            strength=req.strength,
        )
        images = await run_image_workflow(workflow, prefix)
    return JSONResponse(await shape_images(images, req.response_format, PUBLIC_BASE))


@app.post("/v1/images/edits")
async def image_edits(req: ImageEditRequest):
    """Image-to-image. A source image is mandatory here."""
    source = req.image or (req.image_urls[0] if req.image_urls else None)
    if not source:
        raise HTTPException(status_code=400, detail="image is required for edits")
    try:
        width, height = parse_size(req.size)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    async with httpx.AsyncClient(timeout=300) as client:
        init_image = await stage_reference_image(source, client)
        workflow, prefix = await build_image_workflow_async(
            prompt=req.prompt,
            model=req.model,
            width=width,
            height=height,
            seed=req.seed,
            steps=req.steps,
            guidance=req.guidance,
            count=req.n,
            init_image=init_image,
            strength=req.strength,
        )
        images = await run_image_workflow(workflow, prefix)
    return JSONResponse(await shape_images(images, req.response_format, PUBLIC_BASE))


@app.post("/v1/images/edits/upload")
async def image_edits_upload(
    request: Request,
    prompt: str = Form(""),
    model: str = Form(DEFAULT_IMAGE_MODEL),
    n: int = Form(1),
    size: str = Form("1024x1024"),
    response_format: str = Form("url"),
    seed: Optional[int] = Form(None),
    steps: Optional[int] = Form(None),
    guidance: Optional[float] = Form(None),
    strength: Optional[float] = Form(None),
    image: Optional[UploadFile] = File(None),
    mask: Optional[UploadFile] = File(None),
):
    """Image-to-image, multipart upload form (matches the OpenAI Images API spec).

    The previous JSON endpoint could not serve clients that follow the standard
    OpenAI multipart contract — their binary upload crashed FastAPI's validation
    handler with a UnicodeDecodeError before the bytes ever reached the route.
    This endpoint binds the upload to ``UploadFile`` directly so multipart
    requests with a binary ``image`` field are accepted.
    """
    if not prompt.strip():
        raise HTTPException(status_code=400, detail="prompt is required")
    if image is None:
        raise HTTPException(status_code=400, detail="image is required for edits")
    # Reject impossible edit sizes up front; parse_size will produce 0×0 for
    # malformed input, which ComfyUI then accepts and silently ignores.
    try:
        width, height = parse_size(size)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    if width <= 0 or height <= 0:
        raise HTTPException(status_code=400, detail="size is invalid")
    # Sanity cap — ComfyUI will error with a 400 if the resolution exceeds its
    # model limits, but better to fail fast on the bridge than to ship a 60s
    # 4096x4096 request to a 9 GB SDXL checkpoint.
    if width * height > 4096 * 4096:
        raise HTTPException(status_code=400, detail="size exceeds 4096x4096")

    blob = await image.read()
    if not blob:
        raise HTTPException(status_code=400, detail="image is empty")
    # Accept up to ~20 MB; ComfyUI caps uploads at the same ceiling in practice.
    if len(blob) > 20 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="image larger than 20 MB")
    ext = (image.filename or "").rsplit(".", 1)[-1].lower() if image.filename and "." in image.filename else "png"
    if ext not in {"png", "jpg", "jpeg", "webp", "bmp"}:
        ext = "png"
    staged_name = f"ref-{uuid.uuid4().hex[:12]}.{ext}"

    async with httpx.AsyncClient(timeout=300) as client:
        upload = await client.post(
            f"{COMFY_HOST}/upload/image",
            files={"image": (staged_name, blob, image.content_type or "application/octet-stream")},
            data={"overwrite": "true", "type": "input"},
        )
        if upload.status_code >= 400:
            raise HTTPException(
                status_code=502,
                detail=f"ComfyUI rejected the upload: {upload.text[:300]}",
            )

        mask_blob: Optional[bytes] = None
        if mask is not None:
            mask_blob = await mask.read()
            if mask_blob:
                mask_ext = (mask.filename or "").rsplit(".", 1)[-1].lower() if mask.filename and "." in mask.filename else "png"
                if mask_ext not in {"png", "jpg", "jpeg", "webp", "bmp"}:
                    mask_ext = "png"
                mask_name = f"mask-{uuid.uuid4().hex[:12]}.{mask_ext}"
                mask_upload = await client.post(
                    f"{COMFY_HOST}/upload/image",
                    files={"image": (mask_name, mask_blob, mask.content_type or "application/octet-stream")},
                    data={"overwrite": "true", "type": "input"},
                )
                if mask_upload.status_code >= 400:
                    raise HTTPException(
                        status_code=502,
                        detail=f"ComfyUI rejected the mask upload: {mask_upload.text[:300]}",
                    )

        workflow, prefix = await build_image_workflow_async(
            prompt=prompt,
            model=model,
            width=width,
            height=height,
            seed=seed,
            steps=steps,
            guidance=guidance,
            count=n,
            init_image=staged_name,
            strength=strength,
        )
        images = await run_image_workflow(workflow, prefix)
    return JSONResponse(await shape_images(images, response_format, PUBLIC_BASE))


@app.post("/v1/audio/speech")
async def audio_speech(req: SpeechRequest):
    """Speech synthesis through Piper on the Mac Studio."""
    if not req.input.strip():
        raise HTTPException(status_code=400, detail="input text is required")
    try:
        audio, media_type = await piper_synthesize(req)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    except RuntimeError as exc:
        # A voice whose g2pW assets are not cached can fail while a perfectly
        # good default voice would have spoken the same line. Retrying once on
        # the default keeps the feature working instead of surfacing a 500.
        requested = req.voice or DEFAULT_VOICE
        if requested != DEFAULT_VOICE:
            log.warning(f"Voice '{requested}' failed ({exc}) — retrying on {DEFAULT_VOICE}")
            try:
                audio, media_type = await piper_synthesize(
                    SpeechRequest(**{**req.model_dump(), "voice": DEFAULT_VOICE})
                )
            except (FileNotFoundError, RuntimeError) as retry_exc:
                raise HTTPException(status_code=500, detail=str(retry_exc))
        else:
            raise HTTPException(status_code=500, detail=str(exc))
    return Response(content=audio, media_type=media_type)



@app.get("/v1/audio/voices")
async def audio_voices():
    """List the Piper voices installed on this host."""
    voices = await asyncio.to_thread(list_piper_voices)
    return JSONResponse(
        {
            "object": "list",
            "default": DEFAULT_VOICE,
            "data": [
                {
                    "id": voice,
                    "object": "voice",
                    "owned_by": "rhasspy-piper",
                    "default": voice == DEFAULT_VOICE,
                }
                for voice in voices
            ],
        }
    )


@app.post("/v1/chat/completions")
async def chat_completions(req: ChatCompletionRequest):
    """
    Dispatches on the model name: text models go to the LLM router, video
    models go to ComfyUI.
    """
    model_id = (req.model or "").lower()

    # ── Text routing ──
    # ALL text goes to the Mac's Ollama (qwen3:8b). The DGX DeepSeek leg was
    # removed with the DS4 decommission — the DGX is reserved for ComfyUI.
    # "deepseek" stays in the match list only so stale callers naming the old
    # model still land on Ollama instead of falling through to the video path.
    if any(key in model_id for key in ("qwen", "deepseek", "llama", "gpt", "auto", "text")):
        # SSE streaming path. The previous behaviour ignored ``stream: true``
        # for text models and always returned a single JSON blob, which the
        # frontend's streaming reader silently swallowed.
        if req.stream:
            backend = f"ollama/{OLLAMA_MODEL}"
            return StreamingResponse(
                _text_sse_stream(req, backend),
                media_type="text/event-stream",
                headers={
                    "Cache-Control": "no-cache",
                    "X-Accel-Buffering": "no",
                    "x-beeftv-backend": backend,
                },
            )

        try:
            result, backend_label = await ollama_chat_with_label(req)
        except httpx.HTTPError as exc:
            log.error(f"Text backend failed: {exc}")
            raise HTTPException(status_code=502, detail=f"Text backend error: {exc}")
        # Surface which leg served the request as a response header. The body
        # stays exactly OpenAI-shaped so LiteLLM / LangChain do not choke on
        # unknown keys.
        return JSONResponse(result, headers={"x-beeftv-backend": backend_label})

    # ── Video routing ──
    # MiniMax H3 first-render loads ~22 GB of weights and can take 1-3 min
    # before any output appears. The default behaviour is therefore to
    # return a 202 + task_id immediately and let the caller poll
    # ``GET /v1/video/generations/{task_id}`` for status — this is the
    # same contract the dedicated ``/v1/video/generations`` endpoint
    # already exposes, and it lets the frontend show a "queued / loading
    # model" spinner instead of looking dead. A caller that really wants
    # the old SSE-streamed chat.completion response can opt in by sending
    # ``"stream": true`` explicitly.
    is_video_model = any(
        key in model_id
        for key in ("minimax", "wan", "t2v", "i2v", "video")
    )
    if is_video_model:
        # Pull the prompt out of the last user message.
        user_prompts = [message_text(m) for m in req.messages if m.role == "user"]
        if not user_prompts or not user_prompts[-1].strip():
            raise HTTPException(
                status_code=400,
                detail=(
                    f"Video model {req.model!r} requires a non-empty 'user' "
                    "message containing the prompt."
                ),
            )
        actual_prompt = user_prompts[-1].strip()

        # Build the H3 / Wan workflow and submit it directly to ComfyUI.
        # This mirrors the dedicated /v1/video/generations path but keeps
        # the call in-process so we don't recurse through
        # ``_submit_video_task`` (which itself calls ``chat_completions``).
        width = req.width or 1344
        height = req.height or 768
        seed = req.seed or 0
        steps = req.steps or 4
        negative = req.negative_prompt or ""
        client_id = str(uuid.uuid4())

        if "wan" in model_id or "2.1" in model_id:
            log.warning(
                "Wan 2.1 is not installed on the DGX — aliasing to minimax-h3"
            )
            model_id = "minimax-h3"
        if negative:
            log.info(
                f"Dropping unsupported negative_prompt for {req.model} "
                f"(H3 node does not accept it): {negative[:60]}"
            )

        length = max(17, ((req.duration - 5) // 17) * 17 + 5)
        if length != req.duration:
            log.info(
                f"Adjusted length_frames {req.duration} -> {length} for H3 grid"
            )

        workflow = build_minimax_h3_workflow(
            prompt=actual_prompt,
            width=width,
            height=height,
            length_frames=length,
            seed=seed,
            steps=steps,
            shift_video=req.shift_video,
            shift_audio=req.shift_audio,
            turbo_mode=req.turbo,
            filename_prefix="video/BeefTV",
        )

        try:
            prompt_id = await comfy_prompt(workflow, client_id)
            log.info(
                f"[{prompt_id}] Submitted {req.model} job, client={client_id}"
            )
        except Exception as e:
            log.error(f"Failed to submit ComfyUI prompt: {e}")
            raise HTTPException(
                status_code=502, detail=f"ComfyUI error: {e}"
            )

        # Caller asked for the old SSE streaming chat.completion response
        # — fall through to the live stream.
        if req.stream:
            return StreamingResponse(
                sse_stream(prompt_id, req.model, release_vram=True),
                media_type="text/event-stream",
                headers={
                    "Cache-Control": "no-cache",
                    "X-Accel-Buffering": "no",
                    "x-beeftv-backend": f"comfyui/{COMFY_HOST}",
                },
            )

        # Default async response: 202 + task_id, matches
        # ``/v1/video/generations`` so the same poller works.
        body = {
            "id": prompt_id,
            "object": "video.generation",
            "model": req.model,
            "status": "queued",
            "prompt": actual_prompt,
            "poll_url": f"/v1/video/generations/{prompt_id}",
        }
        return JSONResponse(
            body,
            status_code=202,
            headers={"x-beeftv-backend": f"comfyui/{COMFY_HOST}"},
        )

    # Extract prompt from last user message. The previous regex matched only
    # prompts that started with the literal "prompt:" prefix; everything else
    # fell through with the raw user content (including any preamble like
    # "Generate a video of a cat, 10 seconds"). The ComfyUI H3 graph already
    # accepts the full string, so just use the raw prompt verbatim.
    user_prompts = [message_text(m) for m in req.messages if m.role == "user"]
    raw_prompt = user_prompts[-1] if user_prompts else ""
    actual_prompt = raw_prompt.strip()

    # Extract width/height from request or prompt
    width = req.width or 1344
    height = req.height or 768
    seed = req.seed or 0
    steps = req.steps or 4
    negative = req.negative_prompt or ""

    client_id = str(uuid.uuid4())

    # MiniMax H3 is the only video model installed on the DGX, so both video
    # model ids resolve to it. The Wan 2.1 checkpoint is not present, and
    # submitting a graph that names a missing file only produces an opaque
    # ComfyUI validation error several seconds later. Alias explicitly so
    # downstream logs and metrics are honest about which model actually ran.
    if "wan" in model_id or "2.1" in model_id:
        log.warning(
            "Wan 2.1 is not installed on the DGX — aliasing to minimax-h3"
        )
        model_id = "minimax-h3"

    # The H3 node does not accept a negative prompt; if the caller sent one,
    # log it and drop it so the graph validates.
    if negative:
        log.info(
            f"Dropping unsupported negative_prompt for {req.model} "
            f"(H3 node does not accept it): {negative[:60]}"
        )

    # Snap length to the model's 17k+5 frame grid so ComfyUI does not
    # round-trip the value back to the nearest legal step.
    length = max(17, ((req.duration - 5) // 17) * 17 + 5)
    if length != req.duration:
        log.info(f"Adjusted length_frames {req.duration} -> {length} for H3 grid")

    workflow = build_minimax_h3_workflow(
        prompt=actual_prompt,
        width=width,
        height=height,
        length_frames=length,
        seed=seed,
        steps=steps,
        shift_video=req.shift_video,
        shift_audio=req.shift_audio,
        turbo_mode=req.turbo,
        filename_prefix="video/BeefTV",
    )

    try:
        prompt_id = await comfy_prompt(workflow, client_id)
        log.info(f"[{prompt_id}] Submitted {req.model} job, client={client_id}")
    except Exception as e:
        log.error(f"Failed to submit ComfyUI prompt: {e}")
        raise HTTPException(status_code=502, detail=f"ComfyUI error: {e}")

    # Default to streaming on the video path. The first H3 invocation loads
    # ~22 GB of weights and can take 1-3 minutes before any output appears,
    # which without streaming looks like a dead request to the browser.
    if req.stream:
        return StreamingResponse(
            sse_stream(prompt_id, req.model, release_vram=True),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
            },
        )

    # Non-streaming: wait for result, then release VRAM so the next caller
    # (LLM or another render) does not have to wait for an unload.
    try:
        result = await comfy_wait_and_fetch(prompt_id, timeout=600)
        video_url = resolve_output(result.get("outputs", {}))

        try:
            async with httpx.AsyncClient(timeout=30) as client:
                await client.post(
                    f"{COMFY_HOST}/free",
                    json={"unload_models": True, "free_memory": True},
                )
            log.info(f"[{prompt_id}] Released ComfyUI model cache after video job")
        except Exception as exc:  # noqa: BLE001
            log.warning(f"[{prompt_id}] Could not release ComfyUI cache: {exc}")

        if not video_url:
            raise HTTPException(
                status_code=502,
                detail="ComfyUI produced no video output. The model may have failed silently.",
            )

        # Token counts are not available for a video render — ComfyUI does not
        # expose prompt/eval counts the way Ollama does. Emit null counts
        # instead of the previous hardcoded 10/50/60, which silently misattributed
        # usage to whichever caller happened to send ``stream: false``.
        prompt_chars = sum(len(message_text(m)) for m in req.messages)
        estimated_prompt_tokens = max(1, prompt_chars // 4)
        return JSONResponse(
            {
                "id": f"chatcmpl-{uuid.uuid4().hex[:8]}",
                "object": "chat.completion",
                "created": int(datetime.now().timestamp()),
                "model": req.model,
                "choices": [
                    {
                        "index": 0,
                        "message": {
                            "role": "assistant",
                            "content": json.dumps(
                                {
                                    "video_url": video_url,
                                    "prompt_id": prompt_id,
                                    "status": "completed",
                                    "model": req.model,
                                }
                            ),
                        },
                        "finish_reason": "stop",
                    }
                ],
                "usage": {
                    "prompt_tokens": estimated_prompt_tokens,
                    "completion_tokens": None,
                    "total_tokens": estimated_prompt_tokens,
                },
            }
        )
    except TimeoutError as e:
        raise HTTPException(status_code=504, detail=str(e))


# ── NewAPI / WIP endpoint adapter ─────────────────────────────────────────────
# The desktop dev script provisions the MiniMax H3 channel with the
# ``newapi-channel-2`` interface type (POST /v1/video/generations), but the
# proxy only exposes video generation through /v1/chat/completions. Provide a
# thin compatibility shim that translates the WIP request body into a chat
# completion call and translates the SSE response back into a
# /v1/video/generations-shaped task. This keeps the local channel
# configuration in the frontend stable while the proxy remains single-surface.


class _NewAPIVideoRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    model: str = "minimax-h3"
    prompt: str = ""
    seconds: str = "6"
    aspect_ratio: str = "16:9"
    resolution: Optional[str] = None
    generate_audio: bool = False
    image_urls: List[str] = Field(default_factory=list)
    video_urls: List[str] = Field(default_factory=list)
    audio_urls: List[str] = Field(default_factory=list)


def _extract_chat_video_url(payload: Any) -> Optional[str]:
    """Pull a video URL out of an OpenAI-shaped chat completion response.

    The non-streaming video path stores the URL as a JSON string inside
    message.content (e.g. '{"video_url":"http://...","prompt_id":"...',
    ...}'). Parse the JSON first, then fall back to plain text.
    """
    if not isinstance(payload, dict):
        return None
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices:
        return None
    first = choices[0]
    if not isinstance(first, dict):
        return None
    message = first.get("message")
    if isinstance(message, dict):
        text = message.get("content")
        if isinstance(text, str) and text.strip():
            # Try parsing as JSON first (video result from non-streaming path)
            try:
                parsed = json.loads(text)
                if isinstance(parsed, dict) and parsed.get("video_url"):
                    return parsed["video_url"]
            except (json.JSONDecodeError, TypeError):
                pass
            # Fall back to plain text URL
            if text.strip().startswith("http"):
                return text.strip()
    return None


async def _submit_video_task(req: _NewAPIVideoRequest) -> str:
    """Submit a video job to ComfyUI and return the prompt_id immediately.

    The WIP /v1/video/generations client expects the POST to return fast with
    a task ID, then polls GET /v1/video/generations/{id} for status. This
    helper submits the chat-completions streaming variant, reads just the
    first SSE event (which carries the prompt_id), then returns. ComfyUI
    keeps rendering in the background and the GET endpoint reads
    /history/{id} to surface the result.
    """
    if not req.prompt.strip() and not req.image_urls:
        raise HTTPException(status_code=400, detail="prompt or image_urls is required")

    prompt_text = req.prompt.strip() or " "

    log.info(
        f"[newapi-shim] video generations model={req.model} "
        f"seconds={req.seconds} aspect_ratio={req.aspect_ratio} "
        f"images={len(req.image_urls)} videos={len(req.video_urls)} "
        f"audios={len(req.audio_urls)}"
    )

    # Translate seconds → duration (frames) so the chat-completion path picks
    # it up. The video path inside chat_completions snaps the value to the
    # 17k+5 frame grid automatically.
    try:
        seconds_val = int(req.seconds) if req.seconds else 6
    except (TypeError, ValueError):
        seconds_val = 6
    duration_frames = seconds_val * 24  # H3 runs at 24fps

    chat_request = ChatCompletionRequest(
        model=req.model,
        messages=[Message(role="user", content=prompt_text)],
        stream=True,
        duration=duration_frames,
    )

    response = await chat_completions(chat_request)
    if not isinstance(response, StreamingResponse):
        raise HTTPException(
            status_code=502,
            detail="Video submit did not return a streaming response",
        )

    # Consume just enough of the SSE stream to grab the prompt_id. FastAPI's
    # StreamingResponse may flush the body_iterator in arbitrary chunks, so
    # we accumulate until we see at least one full "data: {...}\n\n" event,
    # then break out and drain the rest in the background.
    body_iter = response.body_iterator
    buffer = ""
    prompt_id: Optional[str] = None
    try:
        # Keep reading small chunks (up to ~4 KB) for up to 30s.
        deadline = asyncio.get_event_loop().time() + 30
        while asyncio.get_event_loop().time() < deadline:
            try:
                chunk = await asyncio.wait_for(anext(body_iter, None), timeout=2)
            except (asyncio.TimeoutError, StopAsyncIteration):
                break
            if chunk is None:
                break
            text = chunk if isinstance(chunk, str) else chunk.decode("utf-8", "replace")
            buffer += text
            # A complete SSE event ends with "\n\n"; parse whatever we have.
            for raw_line in buffer.splitlines():
                line = raw_line.strip()
                if not line:
                    continue
                if line.startswith("data:") or line.startswith("data "):
                    payload = line.split(":", 1)[1].strip()
                    try:
                        obj = json.loads(payload)
                    except json.JSONDecodeError:
                        continue
                    pid = obj.get("prompt_id")
                    if pid:
                        prompt_id = pid
                        break
            if prompt_id:
                break
    except Exception:
        pass

    # Drain the rest of the stream in the background so ComfyUI's SSE keepalive
    # is consumed (otherwise uvicorn logs warnings about an unclosed generator).
    async def _drain() -> None:
        try:
            async for _ in body_iter:
                pass
        except Exception:
            pass

    asyncio.create_task(_drain())

    if not prompt_id:
        log.error(
            f"[newapi-shim] No prompt_id parsed from SSE; buffer={buffer[:300]!r}"
        )
        raise HTTPException(
            status_code=502,
            detail="Could not extract prompt_id from the SSE response",
        )
    return prompt_id


async def _run_newapi_video_generations(req: _NewAPIVideoRequest) -> JSONResponse:
    """Adapter that makes /v1/video/generations work like the proxy's
    /v1/chat/completions video path.

    Submits the job to ComfyUI, returns a task ID immediately, and lets the
    client poll GET /v1/video/generations/{id} for the result. This matches
    the WIP /v1/video/generations contract: POST returns fast, GET polls.
    """
    prompt_id = await _submit_video_task(req)

    newapi_body = {
        "id": prompt_id,
        "object": "video.generation",
        "model": req.model,
        "status": "pending",
        "seconds": req.seconds,
        "aspect_ratio": req.aspect_ratio,
    }
    if req.resolution:
        newapi_body["resolution"] = req.resolution
    if req.generate_audio:
        newapi_body["generate_audio"] = True
    if req.image_urls:
        newapi_body["image_urls"] = list(req.image_urls)
    if req.video_urls:
        newapi_body["video_urls"] = list(req.video_urls)
    if req.audio_urls:
        newapi_body["audio_urls"] = list(req.audio_urls)
    return JSONResponse(newapi_body, status_code=202)


@app.post("/v1/video/generations")
async def video_generations(req: _NewAPIVideoRequest):
    return await _run_newapi_video_generations(req)


@app.get("/v1/video/generations/{task_id}")
async def video_generation_status(task_id: str):
    # Check ComfyUI history to get real status and the video URL.
    async with httpx.AsyncClient(timeout=30) as client:
        try:
            history_resp = await client.get(f"{COMFY_HOST}/history/{task_id}")
        except Exception as exc:
            raise HTTPException(
                status_code=502,
                detail=f"ComfyUI unreachable: {exc}",
            )
    if history_resp.status_code != 200:
        raise HTTPException(
            status_code=502,
            detail=f"ComfyUI history returned {history_resp.status_code}",
        )
    history = history_resp.json()
    if task_id not in history:
        # Not in history yet — check queue to distinguish pending vs starting.
        try:
            queue_resp = await client.get(f"{COMFY_HOST}/queue")
            queue = queue_resp.json()
        except Exception:
            queue = {}
        running_ids = {item[1] for item in queue.get("queue_running", [])}
        pending_ids = {item[1] for item in queue.get("queue_pending", [])}
        if task_id in running_ids:
            status = "processing"
        elif task_id in pending_ids:
            status = "pending"
        else:
            status = "starting"
        return JSONResponse(
            {
                "id": task_id,
                "object": "video.generation",
                "status": status,
            }
        )

    # Completed — extract the video URL from ComfyUI outputs.
    outputs = history.get(task_id, {}).get("outputs", {})
    video_url = resolve_output(outputs)
    completed_at = history.get(task_id, {}).get("status", {}).get("completed_at")
    resp_body = {
        "id": task_id,
        "object": "video.generation",
        "status": "succeeded",
    }
    if video_url:
        resp_body["url"] = video_url
    if completed_at:
        resp_body["completed_at"] = completed_at
    return JSONResponse(resp_body)


# ── Start ─────────────────────────────────────────────────────────────────────


if __name__ == "__main__":
    import uvicorn

    log.info(f"Starting BeefTV ComfyUI Proxy on {LISTEN}:{PORT}")
    log.info(f"DGX Spark ComfyUI: {COMFY_HOST}")
    uvicorn.run(app, host=LISTEN, port=PORT, log_level="info")
