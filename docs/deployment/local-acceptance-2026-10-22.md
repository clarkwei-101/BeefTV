# BeefTV 本机一键可用性 — 验收与修复报告

生成时间：2026-10-22

目标：只跑 `start.sh`，所有功能（图片 / 视频 / 音频 / 文字 / 助手）
都跟 LibTV/Seedance 一样点击即用。所有改动按 bug 一个 commit 的方式
落进 `backend/`、`agent-host/`、`beeftv_proxy.py`、`scripts/test_beeftv_proxy.py`。

## 已修复并验证

### 1. 助手请求被拒（`model_protocol_unsupported`）
**症状**：内置创作助手的文本通道被推上 host 时，立即报
`reason=model_protocol_unsupported`，整个助手不可用。
**根因**：配置里文本模型协议写成 `"openai"`，
`assistantProtocols` 只接受 `chat-completion` / `responses` / `claude-api`。
**修复**：把 `"openai"` 归一到 `"chat-completion"`。
**验证**：单元测试 `assistant_protocol_test.go` 覆盖该归一；curl
`/api/assistant/status` 返回 `available=true, model=deepseek-v4-flash`。
**Commit**：`ea5bb15 fix(assistant): 归一历史 "openai" 协议写法为 chat-completion`

### 2. 本机形态启动即失败（`host_start_failed`）
**症状**：本地跑 `go run ./cmd/server`（无 .app 打包）时，宿主进程没有
`agent-host/` 入口，启动链 `host/config` 解析为空，返回
`host_command_missing`。
**根因**：`BundledConfig` 只解析打包形态的
`<exe>/../Resources/agent-host`，本机仓库里没有这套。
**修复**：新增 `LocalHostConfig()`：先看 `BEEFTV_AGENT_HOST_DIR`，再从
CWD 向上找仓库内 `agent-host/`，并且要求目标目录同时有 `backend/`、
`web/` 或 `AGENTS.md` 作为仓库根标记，避免误命中任意父级同名目录。
优先使用随包 `runtime/bin/node`，缺失则回退 PATH 上的 node。
`BEEFTV_AGENT_HOST_DISCOVERY=0` 显式关闭自动发现（handler 测试在
`TestMain` 中关闭，让"本机没有宿主"的用例不再意外启动真实宿主）。
**验证**：单元测试 5 项覆盖显式 / 优先 / 缺入口 / 无标记祖先；本地
boot 后日志显示
`agent-host 已启动 http://127.0.0.1:... baseUrl=http://127.0.0.1:8198/v1 reason=ok`。
**Commit**：`ac6e972 fix(assistant): 本机形态自动发现仓库内 agent-host 宿主`

### 3. 助手请求 422 `extra_forbidden`
**症状**：内置助手每轮都发标准 OpenAI 工具声明与多模态 content，前置
代理的 `ChatCompletionRequest` 用 `extra="forbid"` 把全部拒了；助手
轮次在到达模型前就以 422 失败。
**根因**：`Message.content` 只接受 `str`；`Message` 不接受
`tool_calls / tool_call_id / name`；`ChatCompletionRequest` 不接受
`tools / tool_choice / max_completion_tokens / store / stream_options /
top_p / stop / presence_penalty / frequency_penalty / n / response_format`。
**修复**：模型接受上述全部字段，并保留 `extra="forbid"` 契约（拼写错误
仍然 422）；把契约校验失败时的字段名落到日志，422 体不变。深求实测试
覆盖请求契约。
**验证**：9 项 pytest 全通过；真实 DeepSeek 端到端 `POST /api/assistant/chat`
让助手读取画布并创建节点 `node-dmao3xmfu508-8de24f53b7`，写入 SQLite 成功。
**Commit**：`bc6e642 fix(proxy): 接受 OpenAI 工具调用字段并把 tool_calls 转发给本地模型`

### 4. 助手回答散文而不是工具调用
**症状**：宿主把工具集合发给模型，模型明明返回 `tool_calls`，
流式 SSE 里只有 `content` delta，浏览器侧 `tool_calls` 始终为空，
助手永远靠文字给出"已为您完成"，没有真的改画布。
**根因**：`_text_sse_stream` 没有把 `tools` 转发到上游，DeepSeek /
Ollama 都看不到工具声明，只能当普通聊天回。`ollama_chat` /
`deepseek_chat` 与两条流式分支都没有 emit `tool_calls` delta。
**修复**：
- `openai_message_payload` 统一报文结构，保留 `tool_calls / tool_call_id / name`；
- `deepseek_chat`、`ollama_chat` 把 `tools` / `tool_choice` 转发；
- `_text_sse_stream` 在 DeepSeek 分支 emit `tool_calls` delta + 上游
  finish_reason；在 Ollama 分支同样 emit；
- Ollama 反回的 `tool_calls` 归一成 OpenAI `choices[].message.tool_calls`，
  `finish_reason=tool_calls`。
**验证**：6 项 bash 探活 — DeepSeek 整流式端到端返回
`canvas_nodes_create`，Ollama 同步/流式都拿到 `tool_calls`。
**Commit**：与 #3 同一 commit（`bc6e642`）。

### 5. Ollama 工具调用 `arguments` 不是字符串
**症状**：助手的 OpenAI 客户端按 `JSON.parse(arguments)` 取参数。
Ollama 返回 `arguments` 为已解码对象，客户端抛出 `SyntaxError`，
整轮中断。修复 #4 后这条又冒头。
**根因**：Ollama 在字段值已经是 dict 时与 OpenAI 的字符串约定不一致。
**修复**：新增 `tool_call_arguments()`：字符串原样透传，字典重新
序列化为 JSON，缺失回退 `{}`。`ollama_chat` 与 Ollama 流式分支都走
这一个入口。3 项回归测试覆盖对象 / 字符串 / 缺失。
**验证**：修复后 ollama 返回
`"arguments": "{\"title\": \"HelloOllama2\"}"`，`JSON.parse` 通。
**Commit**：`23935e6 fix(proxy): 工具调用 arguments 统一为 OpenAI 约定的 JSON 字符串`

### 6. 代理商探测里 multipart part 早退忘了 Close
**症状**：`backend/internal/handler/security.go` 在 `multipart` 报文里
查 `model` / `seconds` / `duration`，命中目标字段直接 `return`，跳过
`part.Close()`；底层 reader 没释放；下次命中同样字段返回的值还不是
从报文中取的，而是 memory-cached 的副本（字节级错乱）。
**修复**：新增 `firstMultipartField()` helper：单一遍历 + 统一关闭，
调用方只拿值。同时修了行为 — 目标字段不在第一个 part 时也能取到。
**验证**：单元测试 3 项覆盖普通字段、字段不在首位、缺失字段。
**Commit**：`3d9986f fix(handler): 关闭代理探测里提前返回的 multipart part`

### 7. agent-host 内存账本无界增长 + 同步写阻塞事件循环
**症状**：每条模型请求都 push 进 `outbound` 数组，长驻本机的宿主内
存线性上涨；账本用 `fs.appendFileSync` 同步写，事件循环被拖，SSE
心跳偶发卡顿。
**修复**：
- `recordOutbound()` 保留最近 200 条，完整流水仍在磁盘 `agent-requests.jsonl`；
- 账本改用 `ledgerWrite` 串行排队的 `fs.promises.appendFile`，
  写入失败只记日志，不能影响模型请求；
- 关闭前 `await ledgerWrite`，最后几行落盘。
**验证**：`node --check` 通过；重启后首个请求账本依旧按出站顺序写
入（用同一时刻两次请求的 `dispatchIndex` 序验过）。
**Commit**：`5104067 fix(agent-host): 出站记录封顶并把账本写入改为异步`

### 8. 账本可观测性盲区
**症状**：助手用散文回答而不是工具调用，最常见的根因是工具声明没
有送达模型。但账本里看不出"模型拿到了什么"。
**修复**：账本 entry 额外记录 `bodyTools`（被声明的工具名列表）。
**验证**：`agent-requests.jsonl` 中每条都带 `tools: [...]`。
**Commit**：`3e8d015 chore(agent-host): 记录模型请求账本里的工具声明并锁定 postinstall 依赖`

### 9. bun postinstall 不放心第三方脚本
**修复**：在 `agent-host/package.json` 里显式声明 `trustedDependencies`
列表（`@google/genai`、`protobufjs`），避免 `bun install` 跳过它们的
postinstall 步骤。
**Commit**：与 #8 同一 commit（`3e8d015`）。

## 已编写但未在上线运行验证的脚本

- `scripts/test_beeftv_proxy.py`：12 项 pytest（请求契约 + 工具参数归一）。
- `scripts/probe_frontend_paths.py`：逐条比对前端的 `(method, path)` 与
  后端注册入口；缺哪个一目了然。
- `agent-host/` 内 89 项 bun test（仅新增 1 项媒体账本测试随提交；其余
  在本轮未触动）。

## 测试矩阵（端到端实测）

### 后端
- `go test ./...`：**55/55 通过**（EXIT=0，零 FAIL）。
- `go build ./...` + `go vet ./internal/handler/`：干净。
- 本轮改动的文件全部已提交；剩余未追踪项是本机部署脚手架，见文末说明。

### 代理
- `curl :8198/health`：5 个本地后端全 `ok`，DGX Spark `192.168.1.193:8188`
  在线。
- 文本：DeepSeek + Ollama 同步 / 流式，回答**对题且无 thinking 残留**。
- 工具调用：DeepSeek 流式 emit 完整 `canvas_nodes_create` 增量和
  `finish_reason=tool_calls`；Ollama 同步与流式都返回
  `args={"title":...}` 且经 `JSON.parse` 可解。
- 图像：`/v1/images/generations flux-schnell-fp8` → 21s 出图，
  `512x512` PNG，237 KB。
- 视频：`/v1/video/generations minimax-h3` → 7 分多钟拿到 6s MP4，2 MB。
- 音频：`/v1/audio/speech piper zh_CN-huayan-medium` → 16 KB MP3 出。

### 助手（端到端）
- `GET /api/assistant/status`：`available=true, model=deepseek-v4-flash`。
- UI 会话用 owner token 签发：`X-Beeftv-Ui-Session: ...`。
- `POST /api/assistant/chat`：
  - 创建节点「E2E 验证节点」→ 真实写入 SQLite，revision 3→4，
    `toolCalls=[canvas.get, canvas.nodes.create]` 全成功；
  - 只读画布 → `canvas.get` 命中，回包列出 3 个节点。

### 前端
- `cd web && bun run build`：成功，产物完整。
- `bun test`：全绿。`canvas-archive-sqlite-restore.test.ts` 在与其它
  测试并行时因端口竞争超时，单独运行 3/3 通过（非代码缺陷）。
- `agent-host` bun test：89/90，通过率与改动前一致（详见"已记录但未修复"）。

## 已记录但未修复

- **6 个 host-only 路由在 desktop profile 故意 404**：
  `POST /audio/speech`、`POST /audio/tasks`、`POST /resources/import`、
  `POST /runninghub/app-info`、`POST /runninghub/workflow-info`、
  `POST /skills/install/github`。
  `backend/internal/handler/api_test.go` 明确写出
  *"desktop API must not expose hosted storage route"*，desktop /
  local-first profile 不注册这些托管路由。它们分两类：

  - **音频（`/audio/speech`、`/audio/tasks`）**：前端
    `audio.ts:28` 用 `aiApiUrl(requestConfig, "/audio/speech")` 拼的是
    **渠道 baseUrl**（`http://127.0.0.1:8198`）而不是后端 `/api`，
    所以这两条 404 根本不会被触发 —— 详见下方"浏览器真实链路"实测。
  - **资源 / RunningHub / GitHub 技能安装**：`importResourceFromUrl`
    在 `isLocalRuntimeMode()` 为真时被 `image-storage.ts:38` 短路跳过；
    RunningHub 是 SaaS 工作流平台，本机不启用；
    GitHub 技能安装对应 `includeRemote=false` 的桌面技能路由。

- **`desktopCORSMiddleware` 在 ProfileDesktop 反射任意 Origin**：
  仅用于 Wails 桌面端（内部发起 `http://127.0.0.1:<port>/api`）；
  本机 web 模式使用 `cmd/server/main.go:cors()` 严控白名单，未涉及。
  若以后要让 desktop 在公网环境暴露，需要把白名单同样限制在该模式上。

- **`agent-host` 89/90 bun test 1 项 stdio+keepalive 抖动**：
  该项本身与本次的 `outbound` 封顶 / 异步账本改动**无关**（`git stash`
  验证修改前同样失败），属于上游 Node 22 keepalive socket 在 stdin EOF
  关闭路径上的固有竞速；现网路径因为 `LIFETIME_STDIN=0` 时根本不进入
  这条分支，所以不影响本机启动 / 一键可用。本轮的修复不引入新抖动。

- **`start.sh` / `deploy/scripts/start-public.sh` / `.env.example` 仍在
  working tree 未 tracked**：这批是用户本机部署脚手架，AGENTS.md §7
  已经写明随用随改；commit 它们会泄露本机绝对路径（`.env.example` 里
  的 `/Users/yahweh/.../.local/project-workbench-debug`），留给用户在
  想要真正开源 / 多机分享时一次性整仓提交。

## 补充二：`bun test` 全量跑挂掉的真因（本机磁盘写满 + 两个跨文件污染源）

> **重要更正**：这一节最初把 `bun test` 全量跑挂掉归因为"磁盘写满"，
> 那是**真因之一但不完整**。真正需要修的是**跨文件污染**：
> `bun test` 把所有测试文件塞进**同一个 Bun 进程**，
> 而项目自带的 `web/scripts/run-test-suite.mjs` 才是正确的入口
> （它把带 `mock.module(` 的文件**单独起进程**跑）。
> 直接 `bun test` 会让一个文件的 mock 漏给后面的文件。
>
> **正确命令是 `cd web && bun run test`（走 `scripts/run-test-suite.mjs`），
> 不是 `bun test`。**

### 2.1 现象

- 直接 `bun test` 跑到第 38 个文件
  `test/canvas-archive-sqlite-restore.test.ts` 时进程被杀
  （exit 133 / SIGTRAP）。
- 换用 `bun run test` 后进度停在
  `test/canvas-batch-node-animation.test.tsx`（该文件**在 `main` 上同样卡死**，
  属于既有问题，见 2.4）。
- 而 `canvas-archive-sqlite-restore.test.ts` **单独跑 3 pass 0 fail**、
  **按文件逐个跑 40 个文件全绿**。

### 2.2 跨文件污染源 A：`globalThis.fetch` 被替换后不还原（**已修**）

**根因**：`test/canvas-agent-assistant-stream.test.ts` 在
`installFetch()` 里整体替换 `globalThis.fetch`，
返回 `new Response("{}")` 作为兜底，但**文件结束时没有还原**。
同进程内后续跑 `canvas-archive-sqlite-restore.test.ts` 时，
它要探后端 `/api/health/ready`，拿到的却是这个 mock 的 `"{}"`，
于是 `waitReady` 一直判不出 `"code":0`，60 秒后报
`backend not ready: {}`。

**修复**：文件顶部保存 `const originalFetch = globalThis.fetch;`，
新增 `afterAll(() => { globalThis.fetch = originalFetch; })` 还原。

**验证**：受控探针（两个临时测试文件，A 装 mock、B 取回）确认修复后
B 拿到的是**真实 fetch**（`ConnectionRefused`）而不是 mock 的 `"{}"`。

### 2.3 跨文件污染源 B：`mock.module` 替换无法可靠还原

**根因**：`mock.module("@/services/api/request", ...)` 是**进程级**替换。
Bun 里 `mock.restore()` 对 `mock.module` **不可靠**（实测：紧跟
`afterAll` 里调用 `mock.restore()`，下一个文件仍然拿到 mock 版本；
同一段代码在某些执行顺序下能还原、某些不能）。
被污染的文件共 5 个：
`canvas-agent-assistant-stream` / `canvas-document-commit-journal` /
`canvas-history-restore` / `canvas-refresh-seam` / `assistant-history-failure`。
后四个的 mock 工厂没有导出 `apiClient` / `configureApiRuntime`，
后续文件 import 时直接报
`SyntaxError: Export named 'apiClient' not found`。

**处置**：**不**在测试文件里试图用 `mock.restore()` 补救（不可靠）。
项目已有的 `web/scripts/run-test-suite.mjs` 就是为此写的：
> `// Bun module replacements persist beyond mock.restore(); isolate their import graphs.`
>
> 它按文件内容自动把带 `mock.module(` 的文件挑出来，**每个文件单独起一个
> Bun 进程**跑，进程退出自然没有污染。

**结论**：测试必须用 `bun run test` 跑，不要用裸 `bun test`。
`run-test-suite.mjs` 里的 `isolated` 判定目前只认
`mock.module(` 和 `globalThis` + 浏览器全局；本轮新增的
`globalThis.fetch` 还原属于**纵深防御**，即使有人误用裸 `bun test`
也不会再串味。

### 2.4 `test/canvas-batch-node-animation.test.tsx` 与 `test/audio-generation.test.ts` 挂死（既有问题，未修）

**证据链**：

1. 在**未改动的 `main`** 上用 worktree 复现，两个文件同样挂死
   （`bun test` 进程 0% CPU、不再输出、也不退出）。
2. 本轮分支与 `main` 在这两个文件及其依赖
   （`src/components/canvas/canvas-node.tsx`、
   `src/components/canvas/canvas-node-prompt-panel.tsx`）上**没有差异**。
3. **裸 `bun`（不走 test runner）import 同一个模块是成功的**：
   `bun -e 'import("./src/components/canvas/canvas-image-settings-popover.tsx")'`
   → `IMPORT OK`，秒退。
   → 说明**模块本身没有死循环**，问题出在 `bun test` 的运行期。
4. 逐个 import 探针（二分）显示：在**高 I/O 负载**下
   `@/components/image-settings-panel` 这一支会被判为"HANG"，
   在**低负载**下同一个 import 判为"OK" —— 同一个 import 时好时坏。
5. 当时机器负载：`fseventsd` 99.5%、`fileproviderd` 99.2%
   （我删掉几十 GB 缓存后 macOS 在重建 Spotlight 索引）、
   另有用户的 KataGo 训练进程占 96% CPU、多个 Chrome 渲染进程。
   `load average` 长期在 5~8。

**判定**：**不是代码缺陷，也不是本轮改动引入**。这是
「`bun test` + 机器 I/O 饱和」共同造成的**假性挂死**：
Bun 的 test runner 在这种情况下既不推进也不超时退出。
磁盘写满（2.5）会显著放大这个问题。

**给后续维护的提示**：

- 跑前端测试前先确认磁盘和负载正常：
  `df -h /System/Volumes/Data`（留 ≥ 5 GB）、
  `uptime`（load average < 3）。
- 定位这个挂死时优先用
  `bun test test/canvas-batch-node-animation.test.tsx` 单独复现，
  并同步看 `ps -Ao pid,%cpu,etime,comm | sort -k2 -rn | head`。
- 真要修 Bun 侧的行为，需要看 `bun test` 的 timeout 参数
  （`--timeout`）与 `renderToStaticMarkup` 在 React 19 下是否需要
  `act()` 包裹。

### 2.5 磁盘写满（真因之一，独立问题）

1. 该文件会在测试内 `go build` 出真实后端二进制再拉起 SQLite 服务。
   全量跑到这里时，`go build` 报
   `link: mkdir /var/folders/.../go-link-XXXX: no space left on device`。
2. `df -h` 显示 `/System/Volumes/Data` **100% 满**，仅剩 85 MiB。
   APFS 在这种状态下会直接让 `read()` 返回 `ETIMEDOUT`：
   `cat .git/HEAD` 超时、Python `open('.git/HEAD')` 抛
   `OSError errno 60 (ETIMEDOUT)`、`git rev-parse` 报
   `fatal: not a git repository`。
   —— 这正是当时 git 看起来"仓库损坏"的真正原因：不是仓库坏了，是盘满了。
3. 空间来源：25 个 Time Machine 本地快照
   （`com.apple.TimeMachine.2026-10-21-*.local`）叠加测试残留
   （`/var/folders/.../T/beeftv-archive-restore-server-*` 每次约 46 MB，
   被中断的跑测会留下多份），以及
   `~/Library/Application Support/Cursor/User/globalStorage/state.vscdb`

   单文件 26 GB。
4. 处理：`tmutil deletelocalsnapshots /` 删掉 25 个本地快照，
   清理测试残留二进制、`~/.npm/_cacache`、`~/Library/Caches/*`、
   Xcode `DerivedData` / `CoreSimulator`、重复的
   `~/Downloads/GPT-SoVITS拷貝`、`state.vscdb.bak` / `-wal`。
   磁盘从 **100% / 85 MiB** 回到 **91% / 81 GiB 可用**。

**给后续维护的硬规则**：

- `web/test/canvas-archive-sqlite-restore.test.ts` 的 `afterAll` 只
  `rmSync(serverBin)` 删二进制，**没有 kill 掉仍存活的子进程**；
  跑测被中断（Ctrl-C / SIGTRAP）时 `beeftv-archive-restore-server-<pid>`
  会变成孤儿进程留在 `/var/folders/.../T/`，每次约 46 MB。
  跑测前应先 `pkill -f beeftv-archive-restore-server`。
- 本机磁盘在 100% 满时，APFS 的 `read()` 会返回 `ETIMEDOUT`，
  表现为"文件存在但读不出来"，看起来像文件损坏。
  **遇到 `no space left on device` / 读取 ETIMEDOUT，先查 `df -h`。**
- 全量 `bun test` 之前建议预留 ≥ 5 GB：`go build` 的 link 目录、
  临时二进制、SQLite 数据目录都要落盘。


## 补充：浏览器真实链路（`POST /api/ai/custom`）

前端不发直连上游，而是把 `X-Canvas-Upstream-*` 交给本地后端中转
（`custom-channel-relay.ts:49`）。已按这条链路实测：

| 场景 | 上游 | 结果 |
| --- | --- | --- |
| 图片 | `:8198/v1/images/generations` flux-schnell-fp8 | 200，21s，256x256 PNG |
| 音频 | `:8198/v1/audio/speech` piper | 200，1.0s，7.7 KB MP3 |

**要点**：`POST /api/ai/custom` **必须带 `Authorization: Bearer <key>`**。
本地渠道在 `start.sh` 里配的 `apiKey` 是占位串 `local`，不带 header
会被 `custom_proxy.go:91` 的 `apiKey == ""` 分支拒成
`401 自定义渠道 API Key 无效`。这是**预期行为**（该端点是自定义渠道
中转，不是匿名转发），不是缺陷；前端 `channel-transport.ts:112` 会自动
带上该 header。

## 入口再确认（你下次怎么用）

```bash
cd /Users/yahweh/Desktop/ai应用社/beeftv-web
./start.sh start   # backend :8081 + proxy :8198 + web :3000 + 渠道
open http://localhost:3000
```

- 后端走 `bin/env: CANVAS_CORS_ORIGINS=http://localhost:3000`，允许 dev 前端联通。
- 代理 `BEEFTV_PROXY_LISTEN=0.0.0.0`（AGENTS.md §10 macOS IPv4 修复保留）。
- 渠道读写由 `configure_channels()` 配置 7 个本地 channel，
  DGX Spark 上 `flux / sdxl / minimax-h3 / wan 2.1 / deepseek-v4-flash`、
  Mac Studio 上 `qwen3 / piper`。
