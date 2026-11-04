/**
 * 版本自愈（stale chunk 自愈）
 *
 * 问题：Vercel 边缘节点会按路径缓存 index.html，而且忽略 query string。
 * 每次发版后，老用户浏览器里的 index.html 仍然指向上一版的 chunk 文件名，
 * 而那些文件在新部署里已经被删除，动态 import 于是报
 * 「Importing a module script failed.」，页面永远停在加载态。
 *
 * 方案：启动时把「本次构建的标记」和「服务器上当前部署的标记」比一下。
 * 不一致就说明手上的 index.html 是旧的，强制重载一次拿到新的。
 * 只有一次机会，避免刷新循环。
 */

const STAMP_URL = "/build-stamp.json";
const RELOAD_FLAG = "beeftv:stale-reload";
const MAX_RELOADS = 1;

/** 本次打包生成的标记，由 vite define 注入。 */
function currentBuildStamp(): string {
    try {
        return String(import.meta.env.VITE_BUILD_STAMP || "");
    } catch {
        return "";
    }
}

/** 从服务端取当前部署的标记。取不到就当作「一致」，不打扰用户。 */
async function fetchDeployedStamp(signal: AbortSignal): Promise<string> {
    const response = await fetch(`${STAMP_URL}?t=${Date.now()}`, {
        cache: "no-store",
        credentials: "same-origin",
        signal,
        headers: { accept: "application/json" },
    });
    if (!response.ok) throw new Error(`build stamp request failed: ${response.status}`);
    const payload = (await response.json()) as { stamp?: unknown };
    return typeof payload?.stamp === "string" ? payload.stamp : "";
}

/**
 * 检测到部署版本已更新时强制重载，让浏览器拿到新的 index.html。
 * 返回 true 表示已经发起重载，调用方应停止后续启动流程。
 */
export async function healStaleDeployment(): Promise<boolean> {
    const localStamp = currentBuildStamp();
    if (!localStamp) return false;

    // 已经重载过一次还遇到同样问题，说明不是缓存问题，继续刷新没有意义。
    const reloads = Number(sessionStorage.getItem(RELOAD_FLAG) || "0");
    if (reloads >= MAX_RELOADS) return false;

    const controller = new AbortController();
    const timer = window.setTimeout(() => controller.abort(), 5000);

    try {
        const deployedStamp = await fetchDeployedStamp(controller.signal);
        if (!deployedStamp || deployedStamp === localStamp) return false;

        sessionStorage.setItem(RELOAD_FLAG, String(reloads + 1));
        // 加时间戳绕开 CDN 对 index.html 的缓存。
        const url = new URL(window.location.href);
        url.searchParams.set("_r", Date.now().toString(36));
        window.location.replace(url.toString());
        return true;
    } catch {
        // 网络问题或者标记接口不可用：不做任何事，让主流程继续。
        return false;
    } finally {
        window.clearTimeout(timer);
    }
}
