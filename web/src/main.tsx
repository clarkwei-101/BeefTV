import "@fontsource-variable/inter";
import "@fontsource-variable/jetbrains-mono";
import { bootstrapAppearance } from "@/services/appearance-bootstrap";
import { bootstrapDesktopRuntime } from "@/services/desktop-runtime";
import { hydrateLocalCanvasProjectsFromBackend } from "@/services/local-workspace-repository";
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

async function startApplication() {
    await bootstrapDesktopRuntime();
    // 浏览器缓存晚于后端读入完成时，会用旧连接数组盖掉刚合并的画布。
    await useCanvasStore.persist.rehydrate();
    await hydrateLocalCanvasProjectsFromBackend();
    // 外观自举失败不该挡住应用启动，所以这里只在 finally 里推进，
    // 但动态 import 本身的失败必须冒泡到下面的 catch —— 否则 React 永远不挂载。
    await bootstrapAppearance().catch(() => undefined);
    await import("./application");
}

void startApplication().catch(reportBootstrapFailure);
