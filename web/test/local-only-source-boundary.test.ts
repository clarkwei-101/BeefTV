import { describe, expect, test } from "bun:test";
import { existsSync, readFileSync } from "node:fs";
import { resolve } from "node:path";

const root = resolve(import.meta.dir, "..");

describe("local-only source boundary", () => {
    test("hosted UI and API entrypoints are physically absent", () => {
        for (const path of [
            "src/services/api/wallet.ts",
            "src/services/api/announcements.ts",
            "src/services/user-data-sync.ts",
            "src/pages/auth/login.tsx",
            "src/pages/admin/index.tsx",
			"src/lib/model-pricing.ts",
			"src/constant/credits.tsx",
			"src/pages/settings/channel-video-pricing.tsx",
			"src/components/canvas/canvas-share-modal.tsx",
			"src/pages/canvas/shared.tsx",
			"src/services/api/canvas-share.ts",
			"src/welcome-application.tsx",
			"src/services/api/welcome.ts",
			"src/services/api/channel-order.ts",
			"src/services/api/response-interception.ts",
			"src/services/api/system-update.ts",
			"src/styles/admin-ui.css",
        ]) expect(existsSync(resolve(root, path))).toBe(false);
    });

    // 公网形态有账号系统（后端 /api/auth/*），但它只能是本机后端这一层合同：
    // 不得引入托管版身份、钱包、订单等云端接口。
    test("account surface stays on the local auth contract", () => {
        const authApi = readFileSync(resolve(root, "src/services/api/auth.ts"), "utf8");
        expect(authApi).toContain('"/auth/status"');
        expect(authApi).toContain('"/auth/login"');
        expect(authApi).toContain('"/auth/register"');
        expect(authApi).not.toMatch(/wallet|billing|credits|oauth|user_identit/i);
    });

    test("workspace startup depends only on the local contract", () => {
        const session = readFileSync(resolve(root, "src/lib/user-session.ts"), "utf8");
        const sync = readFileSync(resolve(root, "src/services/local-workspace-sync.ts"), "utf8");
        expect(session).not.toMatch(/auth\/session|remote user|cloud/i);
        expect(sync).not.toMatch(/getRemote|upsertRemote|remoteUserDataPhase|activeRemoteUserId/);
        expect(sync).toContain("flushCanvasStorePersistence");
        expect(sync).toContain("flushAssetStorePersistence");
    });

    test("desktop runtime contains no commerce compatibility state", () => {
        const files = [
            "src/services/workspace-mode.ts",
            "src/stores/use-user-store.ts",
            "src/services/api/creation-runs.ts",
            "src/services/api/task-center.ts",
        ];
        for (const file of files) {
            const source = readFileSync(resolve(root, file), "utf8");
            expect(source).not.toMatch(/billing|creditsEnabled|waiting_payment|pendingPayment|wallet:updated|amountMicrocredits/i);
        }
    });

	test("local model configuration contains capabilities, not commerce metadata", () => {
		for (const file of [
			"src/stores/use-config-store.ts",
			"src/services/api/logical-models.ts",
			"src/components/model-picker.tsx",
			"src/components/image-settings-panel.tsx",
			"src/components/video-settings-panel.tsx",
		]) {
			const source = readFileSync(resolve(root, file), "utf8");
			expect(source).not.toMatch(/billing|microcredits|priceTier|pricePolicy|quoteLogical|showSelectedPrice|showOptionPrices|bypassPriceGuard/i);
		}
	});

	test("production API clients expose no hosted admin endpoints", () => {
		for (const file of ["src/services/api/appearance.ts", "src/pages/plugins/index.tsx"]) {
			const source = readFileSync(resolve(root, file), "utf8");
			expect(source).not.toMatch(/\/admin\//);
		}
	});
});
