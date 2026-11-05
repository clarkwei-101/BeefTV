/**
 * 路由级错误页。
 *
 * 除了展示错误信息，这里还负责处理一类**可自愈**的错误：
 * 「Failed to fetch dynamically imported module」和「Importing a module script failed」。
 *
 * 背景：Vercel 每次发版都会生成新的 chunk 文件名（带内容哈希），旧文件名会被
 * 删除。已经打开站点的用户手里仍是旧版 index.html/JS，路由切换时再去
 * import 旧 chunk 就会 404，页面直接掉进死胡同 —— 只能手动刷新。
 *
 * 这里检测到这类错误就自动整页重载一次：重载会拿到新的 index.html 和新的
 * chunk 映射，页面自动恢复。用 sessionStorage 计数，避免无限刷新循环。
 */
import { Button } from "antd";
import { Home, RefreshCw } from "lucide-react";
import { useEffect } from "react";
import { useNavigate, useRouteError } from "react-router";

import { WorkspaceSignalIcon } from "@/components/ui/aceternity/workspace-signal-icon";

const CHUNK_RELOAD_FLAG = "beeftv:chunk-reload";
const MAX_CHUNK_RELOADS = 2;

/**
 * 判断错误是否由「chunk 加载失败」引起。
 * V8 的报错文案在 Chrome / Edge / Safari 上不完全一致，这里放宽匹配。
 */
export function isChunkLoadError(message: string): boolean {
    return (
        message.includes("Failed to fetch dynamically imported module") ||
        message.includes("Importing a module script failed") ||
        message.includes("error loading dynamically imported module") ||
        message.includes("Failed to load module script")
    );
}

/**
 * 触发一次带时间戳的整页重载，绕开浏览器与 CDN 对 index.html 的缓存。
 * 返回 true 表示已经发起重载。
 */
function reloadWithFreshIndex(): boolean {
    try {
        const reloads = Number(sessionStorage.getItem(CHUNK_RELOAD_FLAG) || "0");
        if (reloads >= MAX_CHUNK_RELOADS) return false;
        sessionStorage.setItem(CHUNK_RELOAD_FLAG, String(reloads + 1));
    } catch {
        // 隐私模式下 sessionStorage 可能不可用：仍然允许重载一次。
    }
    const url = new URL(window.location.href);
    url.searchParams.set("_r", Date.now().toString(36));
    window.location.replace(url.toString());
    return true;
}

export default function RouteErrorPage() {
    const error = useRouteError();
    const navigate = useNavigate();
    const message = error instanceof Error ? error.message : "页面暂时无法显示";
    const recoverable = isChunkLoadError(message);

    useEffect(() => {
        if (!recoverable) return;
        // 短暂延迟，让错误页先渲染出来，避免白屏闪烁。
        const timer = window.setTimeout(() => {
            reloadWithFreshIndex();
        }, 400);
        return () => window.clearTimeout(timer);
    }, [recoverable]);

    return (
        <main className="app-workspace-page grid h-dvh place-items-center px-6 text-foreground">
            <section className="w-full max-w-md text-center">
                <WorkspaceSignalIcon variant="error" size="lg" className="mx-auto" />
                <p className="text-xs font-medium text-muted-foreground">页面运行异常</p>
                <h1 className="mt-3 text-2xl font-semibold">{recoverable ? "正在更新到最新版本…" : "当前页面没有正常加载"}</h1>
                <p className="mt-3 break-words text-sm leading-6 text-muted-foreground">{message}</p>
                <div className="mt-6 flex justify-center gap-3">
                    <Button icon={<RefreshCw className="size-4" />} onClick={() => window.location.reload()}>重新加载</Button>
                    <Button type="primary" icon={<Home className="size-4" />} onClick={() => navigate("/")}>返回主页</Button>
                </div>
            </section>
        </main>
    );
}
