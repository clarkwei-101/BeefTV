import "@fontsource-variable/inter";
import "@fontsource-variable/jetbrains-mono";
// 全局样式必须由入口 chunk 引入：只有这样 Vite 才会把它们写进 index.html 的
// <link rel="stylesheet">，浏览器与 JS 并行加载。
//
// 反例（历史实现）：这三行放在 application.tsx 里，而 application.tsx 是被
// `await import("./application")` 动态引入的。生产构建下 Vite 会把 CSS 变成
// 动态 import 的 preload 依赖，__vitePreload 会等每个 CSS 依赖的 load 事件
// 才真正执行 import()。约 1 MB 的 globals.css 一旦传输卡住（既不报错也不
// 完成），整条启动链就永远挂住，页面停在加载动画上、React 从未挂载。
import "./styles/antd-reset.css";
import "./styles/globals.css";
import "./styles/beeftv-local-overrides.css";
import { bootstrapAppearance } from "@/services/appearance-bootstrap";
import { bootstrapDesktopRuntime } from "@/services/desktop-runtime";
import { hydrateLocalCanvasProjectsFromBackend } from "@/services/local-workspace-repository";
import { healStaleDeployment } from "@/services/stale-deployment-healer";
import { useCanvasStore } from "@/stores/canvas/use-canvas-store";

/**
 * 自举失败时给用户看得见的提示。
 *
 * index.html 用 `#root:empty` 的 CSS 脉冲当加载指示器，React 一旦没挂载，
 * 页面就会永远停在「正在加载」。之前 `void startApplication()` 把所有拒绝
 * 都吞掉了：chunk 404、IndexedDB 打不开、动态 import 失败，用户看到的只有
 * 一个转圈，没有任何线索。这里把失败原因写进 DOM，让用户能判断是刷新、
 * 清缓存还是联系维护者。
 */
function reportBootstrapFailure(error: unknown) {
    const reason = error instanceof Error ? error.message : String(error);
    const root = document.getElementById("root");
    if (!root) return;
    root.innerHTML = "";
    const container = document.createElement("div");
    container.style.cssText =
        "min-height:100dvh;display:flex;align-items:center;justify-content:center;padding:24px;font-family:system-ui,-apple-system,sans-serif;background:#0f0f0f;color:#f4f4f5";
    const box = document.createElement("div");
    box.style.cssText = "max-width:520px;text-align:center;line-height:1.6";
    const title = document.createElement("h1");
    title.style.cssText = "margin:0 0 12px;font-size:18px;font-weight:600";
    title.textContent = "页面加载失败";
    const detail = document.createElement("p");
    detail.style.cssText = "margin:0 0 16px;font-size:14px;color:#a1a1aa;word-break:break-word";
    detail.textContent = reason;
    const hint = document.createElement("p");
    hint.style.cssText = "margin:0 0 20px;font-size:13px;color:#71717a";
    hint.textContent = "通常是网络中断或版本缓存不一致。请刷新页面；若仍然失败，清除本站缓存后重试。";
    const retry = document.createElement("button");
    retry.type = "button";
    retry.textContent = "重新加载";
    retry.style.cssText =
        "cursor:pointer;border:0;border-radius:10px;padding:10px 20px;font-size:14px;font-weight:600;color:#fff;background:linear-gradient(135deg,#6366F1,#8B5CF6)";
    retry.addEventListener("click", () => window.location.reload());
    box.append(title, detail, hint, retry);
    container.append(box);
    root.append(container);
    document.title = "页面加载失败";
}

/**
    动态 import 的硬超时。
 *
    生产构建里 `import("./application")` 会被 Vite 改写成 __vitePreload，
    它要等这次 import 涉及的每个 CSS 依赖触发 load 事件才真正执行模块。
    某个样式表传输卡住（既不报错也不完成）时，import 永远不会 settle，
    页面就永远停在加载动画上。
 *
    即便已经把全局样式挪到入口 chunk，仍可能有别的 CSS 依赖残留，所以这里
    再加一道兜底：超时后 reject，走 reportBootstrapFailure 渲染错误页和
    重试按钮，而不是让用户对着转圈发呆。
 */
const BOOT_IMPORT_TIMEOUT_MS = 20_000;

function importApplicationWithTimeout() {
    let timer: ReturnType<typeof setTimeout> | undefined;
    const timeout = new Promise<never>((_resolve, reject) => {
        timer = setTimeout(() => reject(new Error("应用入口加载超时")), BOOT_IMPORT_TIMEOUT_MS);
    });
    // 无论成功还是失败都清掉定时器：成功时不能让超时在之后突然 reject
    // （未处理的 rejection），失败时也不能留下悬挂的定时器。
    const clear = () => {
        if (timer !== undefined) {
            clearTimeout(timer);
            timer = undefined;
        }
    };
    return Promise.race([import("./application"), timeout]).finally(clear);
}

async function startApplication() {
    // 先确认手上的 index.html 不是旧版本：旧版本会引用已经被删除的 chunk，
    // 导致动态 import 失败。检测到版本过期就先强制重载，别再往下走。
    if (await healStaleDeployment()) return;

    await bootstrapDesktopRuntime();
    // 浏览器缓存晚于后端读入完成时，会用旧连接数组盖掉刚合并的画布。
    await useCanvasStore.persist.rehydrate();
    await hydrateLocalCanvasProjectsFromBackend();
    // 外观自举失败不该挡住应用启动，所以这里只在 finally 里推进，
    // 但动态 import 本身的失败必须冒泡到下面的 catch —— 否则 React 永远不挂载。
    await bootstrapAppearance().catch(() => undefined);
    await importApplicationWithTimeout();
}

void startApplication().catch(reportBootstrapFailure);
