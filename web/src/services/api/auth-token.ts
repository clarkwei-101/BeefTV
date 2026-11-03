// 会话 token 的唯一存放点。浏览器里存在 localStorage 里；localStorage 不可用
// （Bun 测试、隐私模式、SSR）时退回内存，保证同一进程内的请求仍然带得上
// Authorization 头，而不是静默变成匿名请求然后收到 401。

const TOKEN_KEY = "beeftv.auth.token";

let memoryToken: string | null = null;

function storage(): Storage | null {
    try {
        if (typeof localStorage === "undefined") return null;
        return localStorage;
    } catch {
        return null;
    }
}

export function readAuthToken(): string | null {
    const store = storage();
    if (store) {
        try {
            const value = store.getItem(TOKEN_KEY);
            if (value) return value;
        } catch {
            // 读取失败时退回内存。
        }
    }
    return memoryToken;
}

export function writeAuthToken(token: string | null) {
    memoryToken = token && token.trim() ? token : null;
    const store = storage();
    if (!store) return;
    try {
        if (memoryToken) store.setItem(TOKEN_KEY, memoryToken);
        else store.removeItem(TOKEN_KEY);
    } catch {
        // 写不进去也不影响本次会话：内存里还有。
    }
}
