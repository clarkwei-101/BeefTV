import { create } from "zustand";

import {
    getAuthMe,
    getAuthStatus,
    loginAccount,
    logoutAccount,
    readAuthToken,
    registerAccount,
    writeAuthToken,
    type PublicAccount,
} from "@/services/api/auth";
import { ApiError } from "@/services/api/request";

type SessionState = {
    status: "unknown" | "ready";
    needsSetup: boolean;
    account: PublicAccount | null;
    /** 后端运行形态：desktop = 桌面本机（跳过登录守卫），server = 公网（强制登录）。 */
    profile: "server" | "desktop" | null;
    /** false = 公开注册已关闭（登录页隐藏注册入口）。首建管理员时后端恒放行。 */
    registrationOpen: boolean;
    busy: boolean;
    error: string | null;
    ensureSession: () => Promise<void>;
    login: (username: string, password: string) => Promise<void>;
    register: (username: string, password: string) => Promise<void>;
    logout: () => Promise<void>;
    clearError: () => void;
};

export const useSessionStore = create<SessionState>()((set, get) => ({
    status: "unknown",
    needsSetup: false,
    account: null,
    profile: null,
    registrationOpen: true,
    busy: false,
    error: null,
    async ensureSession() {
        if (get().status === "ready") return;
        try {
            const status = await getAuthStatus();
            const profile = status.profile === "desktop" ? "desktop" : "server";
            const registrationOpen = status.registrationOpen !== false;
            // 桌面本机形态：不强制登录，守卫直接放行（后端 server 中间件本来就不挂载）。
            if (profile === "desktop") {
                set({ status: "ready", needsSetup: false, account: null, profile, registrationOpen });
                return;
            }
            if (!status.registered) {
                set({ status: "ready", needsSetup: true, account: null, profile, registrationOpen });
                return;
            }
            const token = readAuthToken();
            // 没有本地 token 时也要试一次：浏览器可能只带着会话 cookie
            // （媒体标签就是靠 cookie 读资源的），那时依然是已登录状态。
            try {
                const me = await getAuthMe();
                set({ status: "ready", needsSetup: false, account: me.account, profile, registrationOpen });
            } catch (error) {
                // 只有明确的 401 才清本地 token：网络抖动不该把用户踢出登录态。
                if (token && error instanceof ApiError && error.status === 401) writeAuthToken(null);
                set({ status: "ready", needsSetup: false, account: null, profile, registrationOpen });
            }
        } catch {
            // 后端不可用：保持未登录态，页面以降级展示，不阻塞首屏。
            set({ status: "ready", needsSetup: false, account: null, profile: get().profile });
        }
    },
    async login(username, password) {
        set({ busy: true, error: null });
        try {
            const result = await loginAccount(username.trim(), password);
            writeAuthToken(result.token);
            set({ busy: false, account: result.account, needsSetup: false });
        } catch (error) {
            set({ busy: false, error: error instanceof Error ? error.message : "登录失败" });
            throw error;
        }
    },
    async register(username, password) {
        set({ busy: true, error: null });
        try {
            const result = await registerAccount(username.trim(), password);
            writeAuthToken(result.token);
            set({ busy: false, account: result.account, needsSetup: false });
        } catch (error) {
            set({ busy: false, error: error instanceof Error ? error.message : "注册失败" });
            throw error;
        }
    },
    async logout() {
        try {
            await logoutAccount();
        } catch {
            // 登出失败也清本地态：token 本就快过期，不阻塞用户。
        }
        writeAuthToken(null);
        set({ account: null });
    },
    clearError() {
        set({ error: null });
    },
}));

// 401 广播：token 过期或被吊销时回到未登录态（不自动跳登录页，由守卫决定）。
if (typeof window !== "undefined") {
    window.addEventListener("beeftv:auth-expired", () => {
        writeAuthToken(null);
        useSessionStore.setState({ account: null });
    });
}
