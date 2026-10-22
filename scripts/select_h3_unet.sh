#!/usr/bin/env bash
# Decide which MiniMax H3 DiT checkpoint the DGX can actually load, and print
# it as a shell assignment for the Mac-side proxy to consume.
#
# The proxy cannot do this itself: it runs on the Mac, the weights live here, and
# no ComfyUI HTTP endpoint on this build reports diffusion_models contents (both
# /object_info and a bare /prompt submission answer identically for a real file
# and a made-up one). So whoever can see the files decides.
#
# Measured on this host (scripts/bench_h3_variants.py): fp8 300.8s vs int8
# 334.8s for the same prompt and seed = 1.113x, so fp8 wins when present.
#
# Usage:  eval "$(bash scripts/select_h3_unet.sh)"   # sets BEEFTV_H3_UNET

set -u
DIR=/home/clarkwei/ComfyUI/models/diffusion_models
INT8=minimax_h3_fl2va_pruned_int8_convrot.safetensors
FP8=minimax_h3_fl2va_pruned_fp8_scaled.safetensors

if [ -f "$DIR/$FP8" ]; then
  echo "BEEFTV_H3_UNET=$FP8"
elif [ -f "$DIR/$INT8" ]; then
  echo "BEEFTV_H3_UNET=$INT8"
  echo "  # fp8 checkpoint absent on this host; falling back to int8" >&2
else
  echo "  # no H3 DiT checkpoint under $DIR" >&2
  echo "BEEFTV_H3_UNET=$INT8"
fi