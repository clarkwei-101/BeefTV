# AGENTS.md

本文件是BeefTV仓库中 AI、自动化工具和协作者的工作约定。用户当前任务优先于本文件；本文件优先于个人习惯。所有结论应能回溯到代码、配置、测试、日志或文档，不用历史印象替代现状。

## 1. 项目边界

BeefTV（`glanderness/BeefTV`）是面向 AI 影视与短剧创作的工作台，当前仍在快速开发。公开接口、数据结构和部署配置可能直接调整；除非任务明确要求，不为旧字段、旧 API 或旧数据增加兼容层。

仓库由几个边界清晰但可独立运行的单元组成：

| 单元 | 技术栈 | 入口 | 责任 |
| --- | --- | --- | --- |
| `web/` | Vite、React 19、TypeScript、React Router、Ant Design、Tailwind、Zustand、TanStack Query | `web/src/application.tsx`、`web/src/router.tsx` | 工作区 UI、画布交互、浏览器缓存、API 调用和模型协议适配 |
| `backend/` | Go 1.25、Gin、GORM、SQLite | `backend/cmd/desktop`、`backend/cmd/server` | 本地工作区 API、持久化任务、资源和外部模型协议适配 |
| `docs/` | Next.js、Fumadocs、MDX | `docs/content/docs/` | 面向用户和开发者的专题文档；构建配置见 `docs/source.config.ts` |

根目录脚本负责 Wails 与本地开发验证。主线不依赖 PostgreSQL、Redis、登录或多租户部署。

## 2. 开始工作前

1. 先读取任务涉及的入口、调用方、配置、锁文件和相邻测试；先理解现状，再决定是否抽象或重构。
2. 使用 `rg` / `rg --files` 搜索，优先并行读取相关文件。不要为了“统一风格”改动无关模块、依赖、格式或用户已有修改。
3. 先形成目标边界：页面负责什么、service 负责什么、handler/service/repository 如何分层、数据和错误如何流动。新增 helper 必须消除真实重复或隔离明确协议，不能只透传参数。
4. 检查 `git status --short`。不覆盖、不回滚、不清理非本次产生的变更；不使用 `git reset --hard`、`git checkout --` 或宽范围删除。
5. 手工编辑使用 `apply_patch`；默认使用 ASCII，业务中文或已有 Unicode 文件除外。注释只解释非直观算法、核心入口、安全边界和降级原因。

## 3. 目录职责和依赖方向

### 前端

- `web/src/pages/`：路由页面及页面私有 hook/组件；页面协调流程，不直接拼装后端协议。
- `web/src/layouts/`：路由级布局、全局浮层和页面壳；不要在页面重复设置全局 body 状态。
- `web/src/components/`：真实跨页面复用的 UI 或交互能力；页面私有组件留在页面目录。
- `web/src/services/api/`：业务 API、模型渠道协议、资源 API；不依赖 JSX、路由或 AntD 提示。
- `web/src/services/`：文件、媒体、同步、缓存和生成任务等跨页面副作用。
- `web/src/stores/`：跨页面状态和持久化配置；页面临时状态留在页面，媒体大对象不进 `localStorage`。
- `web/src/lib/`：纯函数、画布算法、协议转换、设计 token 和可独立测试的基础能力。
- `web/src/styles/globals.css`：变量、重置和必要的第三方覆盖；页面样式优先使用现有 token 或页面样式。

### 后端

- `backend/internal/handler/`：HTTP 入参、本地 workspace context、调用领域端口和统一响应；不放数据库查询。
- `backend/internal/bootstrap/` 与 `backend/internal/localapp/`：本地组合根和窄端口；启动层不得重新暴露巨型 `app.Service` 方法集。
- `backend/internal/task/`、`asset/`、`project/`、`generation/`：本地领域合同与实现；新合同不得反向 import `internal/app`。
- `backend/internal/app/`：跨域编排和尚未拆出的核心实现。Provider、Protocol、自定义渠道和插件是必须保留的本地扩展能力。
- `backend/internal/repository/`：GORM 查询和持久化；不承载业务策略。
- `backend/internal/model/`：结构、枚举和简单模型方法；不调用外部服务。
- `backend/internal/provider/`：模型供应商能力和协议实现。
- `backend/internal/database/`：数据库连接、迁移和连接池。
- `backend/cmd/`：可执行入口、迁移和启动配置；启动参数不得绕过数据目录约束。

调用链应保持为：`HTTP -> handler -> localapp/domain port -> app/domain implementation -> repository/model`；需要模型上游时进入 `generation` / `provider` / `protocol` / `outbound`。

### 插件和文档

- 修改 `docs/` 前确认内容属于专题文档，而不是把长篇说明重新复制到根 README。目录索引见 `docs/index.md`。

## 4. 前端 API 和状态合同

### 后端业务 JSON

业务 API 的唯一调用入口是 `web/src/services/api/request.ts` 导出的 `http`：

- 模块写 `http.get/post/put/patch/delete`，由 `http` 解包 `{ code, data, msg, reason }`。不要再写 `request(apiClient.*)`，也不要再 `axios.create`。
- 二进制或非信封响应用 `http.raw`（CSV、诊断包 zip）。
- `apiClient` 只留给拦截器和 `http` 内部；默认 `VITE_CANVAS_BACKEND_URL || "/api"`。
- `request()` 仍可用于测试信封解包，不是业务模块的调用面。
- 后端成功响应为 `{ code: 0, data: T, msg: string }`；HTTP 200 不等于业务成功，`code !== 0` 必须抛错。失败时用 `ApiError.reason` / `ApiError.code` 判断类型，不要解析 `msg`。错误码见 `web/src/services/api/error-codes.ts` 与 `docs/content/docs/backend/http-api.mdx`。
- OpenAPI 3.0 在 `GET /api/openapi.yaml`。不要从规范生成 TypeScript 客户端来替换 `http` 模块。
- API 模块定义并导出接口类型；页面和 React Query 直接接收解包后的 `data`，不重复访问 `.data.data`。
- 查询参数使用 `compactApiParams` / `serializeApiParams`；取消请求传递 `AbortSignal` 并保留取消语义。
- `FormData` 不手动设置 `Content-Type`，让 Axios 生成 boundary。写路径失败必须向上抛出，不能 `catch { return defaultValue }`。

### 模型渠道和流式请求

- 自定义渠道 URL/Header 仍由 `custom-channel-relay.ts` 的 `channelRequest` 解析；实际发出走 `channel-transport.ts` 的 `createChannelTransport`。image / video / audio 只组协议 payload，不再各自 `axios + channelRequest`。
- 自定义渠道由本地后端 `/api/ai/custom` 中转；重建 headers 时清除 `x-goog-api-key` 和旧的 `X-Canvas-Upstream-Headers`，不得把第三方密钥放入浏览器 URL。
- Provider 特有 payload、响应解包和状态机留在对应 `image.ts`、`video.ts`、`audio.ts`；不要塞进通用 `request.ts`。
- 原始 `fetch` 仅用于媒体 blob/data URL、资源、Worker 或 SSE；必须检查 `response.ok`，传递正确的 `credentials` 和 `signal`。
- 文本任务 SSE 是 `GET /api/tasks/:id/text-events`，游标是递增事件 `id`；断线使用 `Last-Event-ID` 或 `?after=`，不能把任务 ID 当游标。
- 代理只对文本任务和明确的系统模型事件流路径关闭缓冲/缓存/gzip；不要给所有 `/api/` 请求复制长超时和 `proxy_buffering off`。

### 数据、缓存和写路径

- 画布、项目、任务、素材和大 JSON 使用带用户 scope 的 `localforage`；`localStorage` 只保存小型配置、当前 scope 或 UI 偏好。
- 用户切换时隔离 React Query、localforage 和资源缓存；不能让账号之间串数据。
- 后端不可用时的本地缓存是降级，不代表服务端已保存。UI 必须区分“本地缓存成功”和“服务端持久化成功”。
- 生成、激活、审批、权限、删除、上传、配额、账务和密钥相关操作属于强校验写路径；不使用空 ID、默认用户、默认权限或默认额度兜底。
- 素材删除必须先检查项目、画布、任务和其他业务引用；有引用则保留并返回来源，无引用才清理物理对象。物理删除失败不得删除素材记录。

## 5. 后端响应、权限和安全

- Gin 接口统一返回 `{ code, data, msg, reason }`；失败时 HTTP status 和业务 `code` 都应表达真实失败，不把所有错误包装成 200。机器可读原因放在 `reason`，见 `docs/content/docs/backend/http-api.mdx`。
- 所有对象读取、更新、删除都在 service 校验当前用户和资源归属；管理员权限在 service 校验，不依赖前端隐藏按钮。
- 默认拒绝本机、私网和链路本地上游。可信开发主机只能通过 `CANVAS_ALLOWED_PRIVATE_UPSTREAM_HOSTS` 精确放行；不要设置“允许全部私网”来绕过 SSRF 防护。
- 用户 API Key 保存在浏览器本地，任务创建时可能提交给自部署后端；只在可信部署和 HTTPS 下使用真实密钥。日志、错误上报、URL、localStorage 和持久任务正文不得写入敏感 URL、Cookie 或 API Key。
- 生产必须配置明确的 `CANVAS_CORS_ORIGINS`，保持 HTTPS，限制数据库、备份、数据目录和 `.settings-key` 权限；默认关闭公开注册。
- 数据库字段或表变化时同步更新 `docs/content/docs/backend/backend-database.mdx`，不能只改 GORM model。

## 6. 画布、UI 和设计系统

- 画布组件、状态、算法分别放在 `web/src/components/canvas/`、`web/src/stores/canvas/`、`web/src/lib/canvas/`。事件忽略选择器必须覆盖 modal、popover、dropdown 等浮层。
- 画布拖拽、连接、缩放和快捷键要考虑 pointer capture、滚轮冒泡、焦点以及 `data-canvas-no-zoom` / `data-canvas-wheel-scroll` 边界。
- 节点和对象名称要有可发现的铅笔入口并支持单击编辑；双击或右键不能是唯一入口。图片节点保持原始比例，面板不能长期遮挡主要画布空间。
- Ant Design 共性主题和控件状态集中在 `web/src/lib/app-theme.ts` / `AppProviders`。自带外壳的产品弹窗用 `AppModal flush`，侧栏用 `AppDrawer`；不要再复制 `padding: 0` 的 Modal styles。内容外壳仍是 `.ant-modal-container`。
- 第三方覆盖限定在具体组件，不新增全局 `.ant-modal-*`、`.dark .ant-switch-*`、`.ant-checkbox-*` 或 Segmented 状态补丁。新增 CSS 前先搜索同名选择器，回到唯一源规则修改。
- 遵循 `docs/ui-design-system.md` 及项目三层 token：Primitive → Semantic → Component。inline style 优先引用 `var(--token-name)`，不要散落颜色、圆角、阴影和层级字面值。
- 主操作、普通选中、Checkbox/Radio、Switch 是不同颜色角色；持久切换使用 `aria-pressed`，`type="primary"` 只表示当前主要命令。尊重 `prefers-reduced-motion`，键盘导航保留 `:focus-visible`。

## 7. 本地开发、部署和数据目录

- 先阅读 `.env.example` 和对应 Compose 文件。宿主机后端开发必须使用 Git 忽略的 `.local/project-workbench-debug`，通过 `CANVAS_BACKEND_DATA_DIR` 显式指定；不要把 `backend/data` 当作开发账号数据库。
- 本地缓存放 `.local/cache`；不要提交数据库、上传文件、`.env`、真实密钥、构建产物或编辑器配置。
- 宿主机开发：`backend/` 运行 `CANVAS_BACKEND_DATA_DIR=../.local/project-workbench-debug go run ./cmd/server`，`web/` 使用 Bun 和 Vite；不要用 pnpm/npm 覆盖同一套 `node_modules`，也不要提交 `pnpm-lock.yaml` 或 `package-lock.json`。Docker 热更新使用 `docker-compose.dev.yml`；本地构建运行使用 `docker-compose.local.yml`。
- 生产 Compose 使用 `docker-compose.deploy.yml`（PostgreSQL、Redis、backend、web），源码构建可叠加 `docker-compose.build.yml`。公网只暴露 web 的 `3000`，backend `8080` 留在 Compose 网络内。
- 默认不启动 dev server；只有用户明确要求浏览器预览或联调时才启动，并先确认端口、数据目录和现有进程。
- macOS 本机唯一正式应用路径是 `/Applications/BeefTV.app`。需要更新本机应用时只运行 `./scripts/update-local-beeftv-app.sh`；不将 `build/bin/BeefTV.app` 作为可使用版本保留，不复制、重命名或存档额外 `.app` 副本。
- 健康检查只能证明入口可用，不能替代登录、SSE、任务生成和资源访问验证。

## 8. 验证纪律

项目当前默认不自动运行语法检查、类型检查、测试或构建。用户明确要求验证，或改动风险需要验证时，按范围选择最小充分命令，并在交付中如实记录：

- 前端：`cd web && bun run build`；专项测试用 `bun test ...`。UI 退场规则用 `bun run lint`（只禁 antd Empty 和静态 `Modal.confirm`，不是风格检查）。
- 后端：`cd backend && go test ./...`；涉及 PostgreSQL、资源、任务或权限时补对应集成/冒烟路径。
- 旧内置 Agent 已从产品运行面退场：`/agent/*` 路由不再注册，通用任务创建、任务重试和任务 Worker 拒绝 `cloud_agent`、`cloud_agent_step`、`agent_memory_compact`，旧轮次与记忆压缩调度保持停用，历史数据保留。当前分支的替换内核采用 `agent-host/` 中钉选的官方 pi SDK，Go 管理运行时、业务轮次及操作授权；React 使用 `/api/assistant/*`。按 `docs/refactor/pi-official-reuse.md` 核对官方 API 与实际接入路径，不能恢复旧入口或另建模型会话循环。零模型宿主验证、真实业务操作、付费模型验收与正式发布分别记证据。
- 文档站：`cd docs && bun run types:check` 或 `bun run build`。
- UI 变更能浏览器验证时，检查关键路由、明暗主题、滚动、弹窗、空态和核心交互；不能验证时说明替代依据，不把静态阅读或 `git diff` 写成运行验证。

同类失败连续三次时停止盲试，记录现象、已排除项和新假设，再切换路径或请求用户决策。

## 9. 文档与交付

- 根 `README.md` 只保留项目定位、能力概览、快速开始、部署、安全和文档入口；详细专题写入 `docs/content/docs/`。
- 功能与代码地图分别维护在 `docs/content/docs/overview/features.mdx`、`docs/content/docs/backend/code-map.mdx`。尚未确认的实现状态保留在任务或拉取请求中，不写入公开产品文档。
- API、数据表、SSE、资源存储、部署或安全边界变化时同步对应专题文档；不要只改代码和根 README。
- 文档默认中文，不写过期日期，不公开密码、Token、Cookie、真实账号或机器敏感路径。命令、端口、环境变量必须以当前脚本和 Compose 为准。
- Git 提交说明使用 `<type>(<scope>): <业务模块> - <变更摘要>`，`type` 为 `feat|fix|refactor|perf|docs|test|build|ci|chore|revert`。

交付前至少检查：改动是否聚焦、调用方和类型是否同步、错误/权限/数据归属是否完整、必要文档是否同步、验证是否如实说明、是否留下密钥或本地数据。

## 10. Beeftv OpenAI-Compatible Proxy (`beeftv_proxy.py`)

`beeftv_proxy.py` 是宿主机 `:8198` 上的 OpenAI 兼容入口，把 DGX Spark + 本机 Ollama/Piper/ComfyUI 包装成 `/v1/chat/completions` / `/v1/images/*` / `/v1/audio/*` / `/v1/video/generations`。三段已知契约：

- **Ollama text 路径（`ollama_chat`）**：调用 `POST http://127.0.0.1:11434/api/chat`。qwen3 系列是 thinking-mode 模型，**必须**在 payload 里加 `"think": false`，否则模型把所有 token 写到 `message.thinking`，`message.content` 留空，对 OpenAI 客户端呈现"200 OK + 0 字节回答"。代理同时保留 `extract_text_from_ollama_response()` 作为 Ollama 老版本（无 `think` 字段）的 fallback：先剥 `<think>...</think>` 块，再去 thinking 末尾的重复短句模式（"Wait," / "No," / "Let me count" / "Alternatively" / "Maybe" 段头），最后才回退到完整 thinking 字符串或一条"模型只返回了思考过程"的兜底。
- **Video 路径**：`POST /v1/chat/completions` 收到 `model` 含 `minimax` / `wan` / `t2v` / `i2v` / `video` 时，**默认**直接构造 H3 workflow、调 `comfy_prompt()`、返回 `202 Accepted` + `{id, object: "video.generation", status: "queued", poll_url: "/v1/video/generations/<id>"}`。客户端通过 `GET /v1/video/generations/<id>` 轮询；只有显式 `stream: true` 才会切到老的 SSE 心跳。空 user message 抛 `400`。H3 第一次加载 ~22 GB 权重，1-3 分钟内不返回任何内容属正常 — 选 polling 而非阻塞 curl。
- **不要让 `chat_completions` 调 `_run_newapi_video_generations`**：后者内部又调一次 `chat_completions`，会形成无限递归 500。视频分支在 chat 路由里就地处理，**不**复用 newapi 适配器。
- **`/v1/*` 需要代理 token（公网形态已开启）**：`BEEFTV_PROXY_TOKEN` 非空时，中间件要求 `X-Beeftv-Token: <token>` 或 `Authorization: Bearer <token>`，否则 401 `Unauthorized — missing or invalid auth token`。`/health` 与 `/view` 免 token。手工 curl 测代理必须带头，否则会误判成"模型挂了"。
- **本地视频通道的 `protocol` 必须是 `newapi-channel-2`，不能是 `newapi`**：`backend/internal/protocol/builtin.go` 里 `newapi` = "OpenAI Videos"，提交路径是 **`POST /v1/videos`**（multipart/form-data）；`newapi-channel-2` = "NewAPI Video Generations"，提交路径是 **`POST /v1/video/generations`**（application/json）。而 `beeftv_proxy.py` 只暴露 `/v1/video/generations`（`/v1/images/*`、`/v1/audio/*`、`/v1/chat/completions`、`/v1/video/generations`），**没有 `/v1/videos`**。所以协议写错时症状是上游 `404 Not Found`、`耗时=1ms`、`错误来源：上游 HTTP 响应`、类别 `model_missing` —— 看起来像"模型没配"，实际是打到了不存在的路径。`start.sh` 与 `deploy/scripts/start-public.sh` 的视频通道 `modelProfiles[].protocol` 都必须是 `newapi-channel-2`。
- **本地代理通道的 `interfaceType` 由 capability 覆盖，不由通道 profile 决定**：`backend/internal/modelcatalog/admission.go` 的 `resolveLocalModelSelection` 会按 capability 写死 `image → openai-image`、`video → newapi-channel-2`、`audio → openai-audio`、`text → chat-completion`，注释明确说"the local proxy exposes one OpenAI-compatible surface per capability, so we use the capability-appropriate type rather than the upstream channel-specific types"。因此通道 profile 里的 `protocol` 只影响前端契约匹配和快照过滤，真实请求路径由 admission 决定；改视频路由要同时看 admission 的 capability 映射、通道 profile 的 `protocol` 和前端 `web/src/lib/model-protocols.ts`（`STANDARD_PROTOCOLS.video = ["newapi-channel-2", "newapi"]`，`FALLBACK_PROTOCOLS.video = "newapi-channel-2"`）三处。
- **本地通道配置的唯一真源是数据目录里的 `local-model-config.json` 快照，不是数据库**：`backend/internal/modelcatalog/local_snapshot.go` 的 `ParseLocalChannelModels` 读 `config.channels[].modelProfiles[]`；本地模式下 `model_channels` / `channel_models` 两张表是空的。快照由 `os.ReadFile` 每次读取（`backend/internal/workspace/provider_config.go`），**改完 JSON 立即生效，不需要重启后端**；但 `FilterLocalChannelModels` 在 `intent.Protocol != ""` 时会按 `item.Protocol` 过滤，空 protocol 的条目会被跳过 —— 通道 profile 的 `protocol` 写错会表现为"模型选不出来"，而不是报错。
- **代理侧已经把参考图灌进 H3 workflow**（2026-10-28 实测更正）：`beeftv_proxy.py` 的 `stage_reference_images()` 会把 `image_urls` 逐个落到 ComfyUI 输入目录（支持 `data:` URI 与 http(s) URL），`_submit_video_task()` 把暂存后的文件名接到 H3 workflow 的 `MiniMaxH3ImageToVideo`（1 张 → `first_frame`，2 张 → `first_frame` + `last_frame`）或 `MiniMaxH3ReferenceToVideo`（`ref_images.ref_image_N`）节点上。核验方式：查 DGX 的 `GET /history/<prompt_id>`，应能看到 `LoadImage` 节点指向 `ref-*.png` 且 `MiniMaxH3ImageToVideo.first_frame` 有值。改这条链路时先看这两处，不要再按"代理只支持 t2v"的旧结论走。
- **本机桥接（loopback 上游）允许内嵌参考素材，公网渠道仍要求公网 URL**：`backend/internal/generation/media_policy.go` 的 `MediaHydrationPolicyFor` 对 `newapi` / `newapi-channel-2` / `minimax-video` 等接口类型默认 `RequireURL = true`、`PreferURL = true`；但最后一步会检查 `IsLoopbackUpstream(input.Config.BaseURL)`，上游是 `127.0.0.1:8198` 这类本机桥接时把 `RequireURL`/`PreferURL` 一起关掉——桥接与后端同机，能直接解码 data URL，不需要公网可达的素材地址。公网渠道不受影响：素材不可公网访问时后端仍会报"当前模型协议要求公网素材地址，本地工作区请改用支持内嵌素材的模型"（类别 `invalid_params`，在 `backend/internal/generation/media.go` 抛出，`provider_error.go` 负责归一化）。前端 `web/src/services/api/video-provider-newapi.ts` 的 `inline` 分支仍只在 `isBeefAPIEndpoint(baseUrl)` 且命中 `backend/internal/providerpreset/video_contracts.json` 的 `inlineMedia: true` 契约时生效。

启动：`pkill -f beeftv_proxy.py; sleep 1; nohup python3 beeftv_proxy.py > /tmp/beeftv-proxy.log 2>&1 &`。日志在 `/tmp/beeftv-proxy.log`，重启用上面那行；不要写 systemd unit。

## 11. Cursor Skill Hub (`~/.cursor/plugins/local/ai-vfx-hub/`)

AI 视频/图片生成的 12 个 Skill 集中放在 `~/.cursor/plugins/local/ai-vfx-hub/`，插件 manifest 在 `.cursor-plugin/plugin.json`，每个 Skill 一个目录含 `SKILL.md`（YAML frontmatter `name` + `description`）。Cursor 启动后会自动出现在 skill 列表里，agent 看到 `name` 描述命中就 load。

- 写作约定：每个 Skill 必须**可执行**，不能只写背景；至少 1-2 条真实 curl / 命令示范，端点要命中用户本机（`localhost:8198` 走 BeefTV proxy，`192.168.1.193:8188` 走 DGX Spark ComfyUI，`localhost:11434` 走 Ollama）。
- 跨引用零 404：`bundled/<sub-skill>/SKILL.md` 这种相对路径必须真的有目标文件。
- 不可嵌入任何真实 API Key；Ollama / 本机 ComfyUI 不需要 key，第三方 API 只在文档里写 `os.environ['XXX']`。
- 不要覆盖 `~/.cursor/skills/seedance-prompt-en/` 或 `~/.cursor/skills/seedance-prompt-zh/`（已存在，直接引用）。
- 装新 Skill 时同步加到 `README.md` 的快速开始表，否则用户找不到。

Hypit 完整安装会拉 ~10GB 依赖、跑 5-15 分钟，单独派给 background worker，装完报告 frontmatter 校验 + cross-ref 检查。

## 12. DGX Spark 资源调度与 H3 测量纪律

DGX Spark (`192.168.1.193`) 的 ComfyUI (`:8188`) 独占整块 130 GB GPU（DS4 已于 2026-10-26 从本项目退役：`beeftv_proxy.py` 的 DeepSeek 分支、`dgx-deepseek-local` 通道、`ensure_dgx_headroom` 全部删除；文本一律走 Mac Ollama `qwen3:8b`）。约定：

- **DS4 只是从本项目退役，不是从 DGX 上消失**：`beeftv_proxy.py` 的 DeepSeek 分支和 `dgx-deepseek-local` 通道已删除（文本一律走 Mac Ollama `qwen3:8b`），但 DGX 上 `ds4-server` 进程仍在跑并占约 103 GB / 130.6 GB 显存。跑 H3/Wan 前必须 `ssh dgx 'kill <ds4-pid>'` 释放显存，ComfyUI 才装得下 ~22 GB 权重；跑完可用 `bash ~/ds4-deploy/restart-ds4.sh` 拉回。跑视频前先确认 ComfyUI queue 为空。
- ComfyUI 的启动参数以 `comfyui-dgx.service` 的 `ExecStart` 为准（该 unit 挂在 `default.target.wants/` 下，每次开机都会跑）。当前生效参数含 `--use-sage-attention`、`--reserve-vram 4`、无 `--lowvram`，并设置 `Environment=CUDA_HOME=/usr/local/cuda`（Triton 首次编译需要 `cuda_runtime.h`）。headless SSH 下操作 user systemd 需先 `export XDG_RUNTIME_DIR=/run/user/$(id -u)`。
- 判断显存被谁占用：ComfyUI 的 `POST /free` 释放 0 bytes 即说明是别的进程持有，用 `ssh dgx 'nvidia-smi --query-compute-apps=pid,used_memory'` 确认，不要误判为渲染栈问题。
- 测量 H3 加速时注意本机约 20% 的噪声底线（相同配置多次实测 274–449 s）。单次小于 20% 的提升只能表述为“没有变慢”；`scripts/bench_h3_variants.py` 需先 warmup、用不同 seed（相同 seed 会命中 ComfyUI execution cache，得到假的 `0.0s`），耗时取 ComfyUI `history` 时间戳而非客户端轮询。
- 任何 H3 缓存类节点（如 EasyCache）默认不启用：上游 `Comfy-Org/ComfyUI#15326` 确认其缓存判定只看 video tensor，会让 mux 出来的音轨静默损坏（画面 SSIM 仍 0.95，音频 RMS 掉到约 0.50），修复在 0.31.0（`#15390`）。启用必须同时校验逐帧画面与音频 RMS。
- 升级 ComfyUI 会改变渲染行为，任何加速数据都必须标明是哪个版本下测得的，升级后需全部重测（当前 0.42.0，commit `19ae172b`）。

## 13. 公网部署、账号系统与本地代理链路

公网形态由 `deploy/scripts/start-public.sh` + `deploy/scripts/beeftv-public-watchdog.sh` 拉起，常驻端口固定为 **后端 `18080`、Web `13001`、代理 `8198`**（watchdog 默认值；`start.sh` 的 8080/3000 是本地形态，不要混用）。后端与代理只监听本机，公网只暴露 Web。

- **代理被后端 SSRF 拦截 = “生成不了”**：`ValidateOutboundURL` 默认拒绝本机/私网，渠道 `baseUrl` 指向 `http://127.0.0.1:8198` 必须靠 `CANVAS_ALLOWED_PRIVATE_UPSTREAM_HOSTS="127.0.0.1,localhost,192.168.1.193"` 精确放行，禁止打开全局私网开关。后端启动缺这个变量时，报错是 `source: "local_validation"` + “不允许访问本机、内网或链路本地地址”。
- **任务创建参数位置**：`mode` 属于 `input` 里的字段（`input.mode: "image"`），放顶层会被规范化吃掉，报“只接受本机请求”之外的“不支持的生成模式”。手工 curl 建任务必须照前端真实形状发。
- **自定义渠道中转有两套凭据**：登录态 `Bearer <session token>` 与渠道 API Key 是不同头，混用会被判 401（不是路径 allowlist 问题）。`buildApiUrl` 会剥掉 baseUrl 尾部的 `/v1` 再拼路径，所以代理只暴露 `/v1/video/generations` / `/v1/images/generations` 这类带 `/v1` 的路由。
- **`/api/ops/*` 操作层不能只认 loopback**：公网经隧道进来 Origin 是公网域名，未登录必须 403，已登录会话（`SessionAccount`）要放行并按 owner 身份处理；凭据签发类端点（如 `/ops/clients`）仍只接受本机。
- **公网浏览器不能直连 `127.0.0.1:8198`**：代理返回的 `/view?...` 是本机地址，公网取图/取视频会失败（表现为“端口不可用”、图片裂、视频黑屏）。前端拿到的这类 URL 必须改走后端媒体中转再下载/播放。
- **FLUX / SDXL / H3 的文本编码器只吃英文**：中文提示词等于乱码输入，表现为“出图跟想要的完全不一样”。代理的 image / image-edit / video 提交入口要先用本机 Ollama `qwen3:8b` 把提示词翻成英文再送 ComfyUI。
- **cloudflared 两个坑**：本机 `~/.cloudflared/` 里存在命名隧道凭证时，quick tunnel 会加载它并按命名隧道的 ingress 路由，导致所有 Host 404（看日志里的 `cred-file`）；quick tunnel 必须用干净 HOME 启动。另外当前 Cloudflare 账号下**没有任何域名**，命名隧道/固定域名建不了，只能用 Quick Tunnel（地址随重启变化，写入 `/tmp/beeftv_public_url`）。
- **macOS TCC 会让 launchd 拉不起 Desktop 目录下的脚本**：`launchd` 报 `Operation not permitted` 是权限隔离，不是脚本错。常驻脚本要放到 `~/.local/bin/`（`beeftv-public-watchdog.sh` 已搬），并用 `caffeinate -dims` 防休眠。
- **账号系统**：后端已有 `/api/auth/{status,register,login,me}`，`GET /api/auth/status` 返回 `profile` / `registered` / `registrationOpen`；`CANVAS_REGISTRATION_ENABLED=false` 时只允许建首个管理员（其余 403）。`desktop` profile 前端免登录，`server` profile 走会话。写通道配置 `PUT /workspace/model-config` 属写路径，必须带登录 token。
- **媒体标签靠会话 cookie 读资源，不靠 Bearer**：`<img src>` / `<video src>` 不会带 `Authorization` 头，而资源文件接口必须鉴权（不能为了让图片显示就把用户素材变成公开可读）。因此登录/注册会下发 `beeftv_session` cookie（HttpOnly + SameSite=Lax），后端**只在 GET/HEAD 上**接受它，写操作仍然只认 Bearer token —— SameSite=Lax 不会在跨站子资源请求里带上 cookie，所以不引入 CSRF 面。改会话逻辑时不要把 cookie 接到写路径上。
- **本地 schema 必须含 `auth_sessions`**（v14 迁移），旧断言“本地 schema 不能有 auth_sessions”已过时，改测试时按新契约走。v14 迁移会在 `AutoMigrate` 前删掉"缺 `token_hash` 列"的旧托管版 `auth_sessions`（v2 的删除可能因历史预览分支占用版本号而没真正执行过），否则旧行的空 `token_hash` 会撞唯一索引。
- **健康检查必须放行整个 `/health` 前缀**：`backend/internal/handler/auth_middleware.go` 的 `isReadOnlyPublicPath` 早先只放行精确的 `/health`，而真实路由是 `/health/live`、`/health/ready`、`/health/startup`（`backend/internal/bootstrap/status.go`、`backend/cmd/server/system_status.go`）。结果是探针拿到 401，`deploy/scripts/start-public.sh` 与 `start.sh` 的 `curl -sf .../api/health/live` 失败，watchdog 把健康的后端误判成 DOWN 并反复重启。
- **本地任务的模型选择走 `logicalModelId`**：公网形态下手工建任务必须在顶层带 `logicalModelId`（本地模型形如 `minimax-h3-local::minimax-h3`），`input.config` 里只留尺寸/时长等参数。缺了它、又没有 `input.config.channelId` 时，admission 会返回 `必须指定系统渠道和模型`（`invalid_model_selection`）。另外 `input.config.videoSeconds` 是**字符串**字段，传数字会在解析时失败（"任务输入解析失败：json: cannot unmarshal number into Go struct field"）。

## 14. 公网 watchdog 的运维细节

- **watchdog 副本在 `~/.local/bin/` 时不能用相对路径定位仓库**：`cd "$(dirname "$0")/../.."` 在 `~/.local/bin/` 下会指向 `~/.`，导致构建/启动全部失败。现已改为脚本内硬编码 `REPO_DIR="/Users/yahweh/Desktop/ai应用社/beeftv-web"`；仓库里的 `deploy/scripts/beeftv-public-watchdog.sh` 改动后必须 `cp` 到 `~/.local/bin/` 同步。
- **watchdog 自己会编译后端**：`ensure_backend()` 发现 `/tmp/beeftv-server` 不存在时才 `go build -o /tmp/beeftv-server ./cmd/server`。所以升级后端要**先覆盖 `/tmp/beeftv-server` 再杀旧进程**，否则 watchdog 会拿旧二进制拉起，表现为"改了没生效"。
- **隧道地址轮换要重启后端**：Quick Tunnel URL 变了以后后端 `CANVAS_CORS_ORIGINS` 里还是旧地址，公网浏览器会撞 CORS。watchdog 的 `ensure_tunnel()` 需比对新旧 URL，变化时 `pkill` 后端再拉起以载入新地址。
- **macOS quarantine 也会拦 `/tmp` 下的二进制**：`xattr -d com.apple.quarantine` 要同时对脚本和 `/tmp/beeftv-server` 执行，否则报 `Operation not permitted`（看起来像 launchd 权限问题，其实是 TCC/quarantine）。
- **只保留一个 watchdog 实例**：launchd 的 `com.beeftv.public-watchdog` 拉起的是权威实例，手工 `nohup` 起的副本会重复抢端口；发现重复时杀掉手工那个，让 launchd 重新拉起。
- **`pgrep -f beeftv-public-watchdog.sh` 数出来的"多个实例"可能是假的**：watchdog 用 `( ... nohup 后端 & )` 这类写法拉子进程时，bash 会 fork 一个辅助子 shell，它**继承脚本的 argv**，于是 `pgrep -f` 会把它算成第二个 watchdog。判据看 cwd 和父子关系：辅助子 shell 的 cwd 是 `RUNTIME_DIR`（`~/.local/share/beeftv`）且是后端的父进程，权威实例的 cwd 是仓库目录。根治办法是子进程用 `exec` 收尾（辅助子 shell 直接变成后端，argv 里不再有脚本名），这样 `pgrep` 数量与真实实例数一致。已改：`ensure_backend()` / vite 回退分支都用 `exec env ... nohup ...`。
- **单实例锁只能靠锁目录 + 存活 PID 判断**：锁的 `mkdir` 是原子操作，锁目录里的 pid 文件必然属于唯一持有者。不要再用 `pgrep -f` 去"确认还有没有别的实例"——它会匹配 watchdog 自己 fork 出去的辅助子 shell，导致新实例永远起不来。`release_lock()` 只删自己持有的锁（旧版本无条件 `rm -rf`，抢锁失败的实例退出会把胜出者的锁删掉，第三个实例再拿到锁，线上出现两个 watchdog 互抢端口）；主循环每轮用 `lock_owned_by_self()` 自检，锁被抢走就退出。锁逻辑的隔离测试用**真实进程**模拟第二个实例：macOS 自带 bash 3.2 没有 `BASHPID`，子 shell 里的 `$$` 仍是父 shell 的 PID，会把"输掉锁的实例"伪装成持有者。
- **视频任务卡在 `starting` 的常见根因是显存而不是路由**：H3 需要约 22 GB 权重，DS4 占着约 103 GB 时任务永远停在 `starting`。先 `ssh dgx 'kill <ds4-pid>'`，再看 `POST /free` 是否真的释放了字节（释放 0 字节说明还被别的进程占着）。
- **watchdog 读不到 `~/Desktop`，前端包必须另行发布**：launchd 拉起的 watchdog 在 TCC 下读不到仓库（代理脚本同步会打印"代理脚本同步失败（沿用旧副本）"），`stage_web_build` 同样读不到 `web/dist`，于是**静默沿用旧副本**——线上跑的还是上一次构建，日志里也看不出报错。发布前端要用普通终端执行 `./deploy/scripts/publish-public-web.sh`（构建 → rsync 到 `~/.local/share/beeftv/web` → reload nginx）。后端二进制不受此限：`/tmp/beeftv-server` 在仓库外，watchdog 读得到，只是升级时必须**先覆盖二进制再杀旧进程**。
