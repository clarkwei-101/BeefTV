import { readFileSync } from "node:fs";
import { resolve } from "node:path";

import { describe, expect, test } from "bun:test";

const routeErrorSource = readFileSync(resolve(import.meta.dir, "../src/pages/route-error.tsx"), "utf8");
const healerSource = readFileSync(resolve(import.meta.dir, "../src/services/stale-deployment-healer.ts"), "utf8");
const mainSource = readFileSync(resolve(import.meta.dir, "../src/main.tsx"), "utf8");

describe("chunk 加载失败自愈", () => {
    test("路由错误页识别 chunk 类错误并触发整页重载", () => {
        expect(routeErrorSource).toContain("Failed to fetch dynamically imported module");
        expect(routeErrorSource).toContain("Importing a module script failed");
        expect(routeErrorSource).toContain("reloadWithFreshIndex");
        expect(routeErrorSource).toContain('url.searchParams.set("_r"');
    });

    test("路由错误页的重载次数有上限，不会陷入刷新循环", () => {
        expect(routeErrorSource).toContain('const MAX_CHUNK_RELOADS = 2;');
        expect(routeErrorSource).toContain("sessionStorage.getItem(CHUNK_RELOAD_FLAG)");
    });

    test("全局兜底监听 error 与 unhandledrejection，覆盖组件内的动态 import", () => {
        expect(healerSource).toContain("export function watchChunkLoadFailures()");
        expect(healerSource).toContain('window.addEventListener("error"');
        expect(healerSource).toContain('window.addEventListener("unhandledrejection"');
    });

    test("全局兜底在应用启动时安装", () => {
        expect(mainSource).toContain("watchChunkLoadFailures()");
    });
});
