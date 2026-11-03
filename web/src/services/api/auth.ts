import { http } from "@/services/api/request";
import { readAuthToken, writeAuthToken } from "@/services/api/auth-token";

export type PublicAccount = {
    id: string;
    username: string;
    role: "admin" | "user";
    status: "active" | "disabled";
    createdAt: string;
};

export type AuthResult = {
    account: PublicAccount;
    token: string;
    expiresAt: string;
};

export type AuthStatus = {
    registered: boolean;
    account: { account: PublicAccount } | null;
    profile: "server" | "desktop" | string;
    /** false = 公开注册已关闭（老账号仍可登录，首个管理员照常可建）。 */
    registrationOpen?: boolean;
};

export { readAuthToken, writeAuthToken };

export function getAuthStatus(signal?: AbortSignal) {
    return http.get<AuthStatus>("/auth/status", { signal });
}

export function registerAccount(username: string, password: string, signal?: AbortSignal) {
    return http.post<AuthResult>("/auth/register", { username, password }, { signal });
}

export function loginAccount(username: string, password: string, signal?: AbortSignal) {
    return http.post<AuthResult>("/auth/login", { username, password }, { signal });
}

export function logoutAccount(signal?: AbortSignal) {
    return http.post<{ loggedOut: boolean }>("/auth/logout", {}, { signal });
}

export function getAuthMe(signal?: AbortSignal) {
    return http.get<{ account: PublicAccount }>("/auth/me", { signal });
}
