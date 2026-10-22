# MiniMax H3 / Wan 2.1 在 DGX Spark 上的推理加速实测报告

- 硬件：NVIDIA DGX Spark（GB10，sm_121，128GB 统一内存）
- 服务端：ComfyUI 0.30.0 @ `192.168.1.193:8188`，torch 2.13.0+cu130，triton 3.7.1
- 客户端：`beeftv_proxy.py`（Mac，`localhost:8198`）
- 启动参数：`--listen 0.0.0.0 --port 8188 --bf16-vae --preview-method auto --reserve-vram 4 --use-sage-attention`
- 原始数据：[`h3-acceleration-results.json`](./h3-acceleration-results.json)

## 一、测量方法（为什么这些数字可信）

加速对比最容易出错的地方不是"跑得慢"，而是"测出一个不存在的加速"。本次踩到的三个坑都
不会报错，只会安静地给出漂亮但错误的数字，因此方法本身必须先说清楚：

1. **计时口径**。所有时长取自 ComfyUI 自己的 history 时间戳（`max(ts)-min(ts)`），不是代理的
   轮询循环。走代理会把客户端轮询间隔算进耗时。
2. **冷启动隔离**。H3 要加载约 22GB 权重。每个变体先跑一次 warmup（不计入），再测第二
   次，否则首次加载会淹没真实推理时间。
3. **避开 execution cache**。warmup 与 measured 用不同 seed。用同一个 seed 时 ComfyUI 会
   命中节点缓存，history 只剩一条消息，算出来是 `0.0s` —— 两侧都是 0，比值毫无意义。这个
   bug 在实测中真实发生过（见下方"被拒绝的假数据"）。
4. **失败即失败**。渲染失败的返回几乎是瞬时的，如果不显式拦下，会被算成 100 倍以上的
   "加速"。脚本现在按 `status != success` 判定为失败并拒绝计入比值。

## 二、加速前后对比

图固定为：1344×768、121 帧、`res_multistep / simple` 采样器、同一 prompt、同一 seed。

| 变体 | DiT 权重 | Attention | 时长 | 相对 int8 加速 |
| --- | --- | --- | ---: | ---: |
| 基线 | int8 ConvRot | PyTorch SDPA | **340.4 s** | 1.000× |
| fp8 | fp8 scaled | PyTorch SDPA | **300.8 s** | **1.113×** |
| SageAttention | int8 ConvRot | SageAttention | **321.8 s** | **1.058×** |
| **出厂配置（实测）** | **fp8 scaled** | **SageAttention** | **284.5 s** | **1.196×** |
| TorchCompile | int8 | PyTorch | — | **失败，见第四节** |

两项加速作用于不同环节（一个换 GEMM 精度，一个换 attention kernel），互不覆盖。出厂组合的
收益为 **1.196×**（340.4 s → 284.5 s），略高于两项单独收益相乘的推算值 1.113 × 1.058 ≈
1.18×。即一次 5.17 秒视频的端到端渲染，从约 5 分 40 秒降到约 4 分 45 秒。

出厂配置的 284.5 s 是在真实代理链路上跑出来的（`beeftv_proxy.py` → DGX ComfyUI），而非
基准脚本直接提交，权重选择走的是 `scripts/select_h3_unet.sh` 的实际路径。

### 一个反直觉的发现：SageAttention 的收益并不来自它自己

单独拿 kernel 微基准对比 sageattn 与 PyTorch SDPA，在 H3 实际用到的形状上**没有稳定收益**：

| 形状 | sage | SDPA | 比值 |
| --- | ---: | ---: | ---: |
| 4k token, 16 head, hd64 | 0.812 ms | 0.770 ms | 0.95× |
| 8k token, 16 head, hd64 | 2.973 ms | 3.048 ms | 1.03× |
| 16k token, 16 head, hd64 | 10.867 ms | 12.225 ms | 1.12× |
| 4k token, 8 head, hd128 | 0.887 ms | 0.909 ms | 1.02× |
| 8k token, hd64, **bf16** | 3.045 ms | 2.989 ms | 0.98× |

也就是说上游 PR 声称的收益只出现在 16k token 以上，而 H3 主要工作在 2–8k 区间，那里基本是
持平甚至略负。端到端仍测到 1.058×，说明这部分收益来自 8k 以上 token 的小胜累积，而不是
"attention 变快了"。因此**不要把 SageAttention 的收益外推**到其它模型或其它长度上。

另外 bf16 反而慢 0.98×，而 H3 正是 bf16 权重 —— 这也是收益被限制的原因之一。

## 三、被拒绝的假数据

以下三个都曾经或很容易产生"看起来极快"的错误结论，记录在此以免重犯：

| 现象 | 真相 |
| --- | --- |
| 两侧都测出 `0.0s`，比值 1.0 | warmup 与 measured 同 seed，整图命中 execution cache |
| `torch.compile` 报出 **139.5× 加速** | 渲染实际是 **失败**（1–2 秒返回），脚本未拦失败状态 |
| 同一 seed 的历史里出现"0 秒渲染" | 不是加速，是缓存；必须换 seed 重算 |

## 四、未采用项清单

| 项目 | 状态 | 理由 |
| --- | --- | --- |
| **torch.compile / Inductor** | **不可用** | 阻塞点是 **环境缺 `python3-dev`**（`/usr/include/python3.12/Python.h` 不存在），需要 sudo 密码，无法非交互安装。**不是硬件问题** —— 见下 |
| **TensorRT / torch_tensorrt** | 未采用 | 本机完全未安装（`ModuleNotFoundError`）。GB10 为 sm_121，TensorRT 对该架构的 kernel 支持缺少公开可用记录，且 ComfyUI 的 H3 路径不经过 TensorRT 节点，投入产出不成立 |
| **TeaCache / CacheGen** | 未采用 | 属于 EasyCache 同类的缓存复用，同样在 H3 的音频分支上有已知的跨调度错缩放风险（ComfyUI#15326），与下面被禁用的项同源 |
| **EasyCache** | **主动禁用** | 见第五节 |
| **VAE tiling** | 未采用 | H3 输出 1344×768×121 帧，bf16 VAE 在 128GB 统一内存上不构成显存瓶颈；实测 `/health` 报告 `degraded: []`，没有 OOM 压力可解，收益无法测量 |
| **Wan 2.1 加速** | **无法实测** | DGX 上 `models/wan_i2v/` 是空目录，Wan 权重从未下载。代理对 `wan` 请求已显式 alias 到 `minimax-h3`（`beeftv_proxy.py`，"Wan 2.1 is not installed on the DGX"），因此本机不存在可加速的 Wan 路径 |
| **fp4 / 更激进的量化** | 未采用 | H3 未提供官方 fp4 权重；自行量化需完整验证音画同步与运动连贯性，超出"不允许回归"的边界 |

### 关于 torch.compile：三次误判的澄清

这块的失败原因被系统性地误读过三次，值得单独记录：

1. 初次报错 `InductorError: SubprocException`，看起来像"sm_121 不支持 Inductor"。
   **证伪**：独立 Triton kernel（`@triton.jit`）在这台机器上编译运行完全正常，sm_121 目标可用。
2. 真实第一阻塞点是 **`CUDA_HOME` 缺失** —— Triton 的 `set_driver_to_gpu()` 会调 gcc 编译
   `cuda_utils.c` 并 `include cuda_runtime.h`，环境里没这个变量，编译死在子进程里。DGX 上
   toolkit 其实完整（`/usr/local/cuda-13.0/targets/sbsa-linux/include`）。导出
   `CUDA_HOME=/usr/local/cuda` 后该错误消失。
3. 剩余唯一阻塞点是 Inductor 的 **C++ wrapper 需要 `<Python.h>`**，而本机未装 `python3-dev`，
   且无免密 sudo。该 wrapper 是 CPU 侧代码生成，对纯 GPU 负载是多余开销，本应可以关掉，
   但 ComfyUI 的 `TorchCompileModel` 节点在**运行期**把 `config.cpp_wrapper` 置回 `True`，
   `TORCHINDUCTOR_CPP_WRAPPER=0` 不生效。

结论：`BEEFTV_H3_TORCH_COMPILE` 保持**默认关闭**。它不是"开了会变慢"，而是"在本机上开不起来"。

## 五、主动禁用：EasyCache（会静默损坏音频）

EasyCache 按**视频流**的残差变化率决定跳过哪些 step，再把缓存的残差**重放到音频**上。
但 H3 的音频与视频走不同调度（音频速度按 `d(σ_a)/d(σ_v)` 缩放），跨 step 重放时残差被错误缩放。

本项目的 H3 图**总是**把音频 mux 进输出 MP4（node 15/16），因此这会产出一个"画面看着
完全正常、声音是错的"的文件。实测表现：音乐类提示词下音频 RMS 掉到约 0.50、谱质心整体
上移（低频直接消失），而**同一批次的 video SSIM 仍有 0.95** —— 只做画面审查发现不了。

代码层面已拒绝：即使设置了 `BEEFTV_H3_EASYCACHE_THRESHOLD`，代理也会拒绝启用并打日志，
除非显式设置 `BEEFTV_H3_EASYCACHE_ALLOW_AUDIO_LOSS=1`。

## 六、兼容性回归

加速改动全部集中在 H3 图构建函数（`build_minimax_h3_workflow`）内，`beeftv_proxy.py` 的
改动仅 3 个 hunk、27 行新增，未触及任何其它路径。以下为实测结果：

| 链路 | 结果 |
| --- | --- |
| `/health` | `status: ok`，`degraded: []`，5 个后端全部 ok |
| `/v1/models` | 7 个模型正常返回 |
| `/v1/audio/voices` | 3 个音色 |
| 文字（Ollama qwen3:8b） | 正常返回 |
| 文字（DeepSeek v4） | 正常返回（thinking 文本为既有行为，非本次引入） |
| 图片（FLUX fp8） | 512×512 PNG 正常下载 |
| 音频（Piper TTS） | MP3 正常，1.47 s |
| 视频（H3 fp8 + SageAttention） | 1344×768、124 帧 + AAC 立体声、5.167 s，音轨 mean −13.9 dB、无静音段 |

最终验收用的那条 H3 视频（fp8 + SageAttention 出厂配置）：2.63 MB MP4，音轨 mean −13.9 dB，
画面为黄昏海滩上飞行的红色风筝，无花屏、无色偏。

## 七、如何复现

```bash
# 1. 在 DGX 上启动 ComfyUI（会自动带上 CUDA_HOME）
ssh dgx '/home/clarkwei/restart_comfy.sh --listen 0.0.0.0 --port 8188 \
  --bf16-vae --preview-method auto --reserve-vram 4 --use-sage-attention'

# 2. 选出实际存在的 H3 权重（fp8 存在则选 fp8）
ssh dgx 'bash /home/clarkwei/select_h3_unet.sh'

# 3. attention A/B（两个变体各需一次 warmup + 一次计时，约 22 分钟）
python3 scripts/bench_h3_attention_ab.py --variants pytorch,sage

# 4. 权重 / 编译组合对照（约 33 分钟 / 变体）
python3 scripts/bench_h3_variants.py --variants int8,fp8,int8_compile

# 5. 启动代理
BEEFTV_H3_UNET=minimax_h3_fl2va_pruned_fp8_scaled.safetensors \
  BEEFTV_PROXY_LISTEN=0.0.0.0 python3 beeftv_proxy.py
```

注意 macOS 上必须显式 `BEEFTV_PROXY_LISTEN=0.0.0.0`：默认的 `"::"` 在 macOS 上会拒绝 IPv4，
导致端口处于 LISTEN 但 `127.0.0.1:8198` 连接被拒。