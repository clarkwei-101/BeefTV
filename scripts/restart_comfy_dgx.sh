#!/usr/bin/env bash
# Detached ComfyUI launcher for the DGX Spark.
#
# Two requirements this file exists to satisfy:
#
# 1. It must return immediately. The server keeps running after this script
#    exits, so an ssh caller waiting on the channel is not held for the
#    lifetime of the process.
#
# 2. CUDA_HOME must be set. torch.compile (inductor) shells out to gcc to build
#    Triton's cuda_utils.c helper against cuda_runtime.h. Without CUDA_HOME that
#    compile dies inside a subprocess and is re-raised as a generic
#    InductorError, which reads like "this GPU architecture is unsupported" when
#    the real cause is a missing header. The toolkit is present at
#    /usr/local/cuda on this host; only the env var was missing.
export CUDA_HOME=/usr/local/cuda
export PATH="$CUDA_HOME/bin:$PATH"

# 3. Inductor's C++ wrapper needs <Python.h>, and python3-dev is not installed
#    on this host (no sudo available to add it). That wrapper is CPU-side
#    codegen, so for a GPU workload it is pure overhead: with it disabled,
#    inductor emits Triton kernels directly and compiles cleanly on GB10.
export TORCHINDUCTOR_CPP_WRAPPER=0

# 4. Inductor's default compile pool forks worker subprocesses, and those
#    children start without an initialized CUDA context, so
#    triton_helpers.set_driver_to_gpu() dies in triton/runtime/driver.py
#    ("self._active = self.default") and surfaces as InductorError:
#    SubprocException on a Triton kernel that compiles fine in the parent.
#    compile_threads=1 makes inductor compile in-process, where CUDA is
#    already live. The tradeoff is slower compilation, not slower inference.
export TORCHINDUCTOR_COMPILE_THREADS=1

cd /home/clarkwei/ComfyUI || exit 1

pkill -f "main.py --listen" 2>/dev/null
sleep 5

setsid ./venv/bin/python main.py "$@" \
  >> /home/clarkwei/.local/share/comfyui-dgx.log 2>&1 < /dev/null &
disown
exit 0