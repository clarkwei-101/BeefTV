import { expect, test } from "bun:test";
import { channelHasGenerationCredential, channelHasManagedBeefAPICredential, isBuiltinBeefAPIChannel } from "../src/stores/use-config-store";
import { beefAPIVideoContract } from "../src/lib/beefapi-video-contracts";

// The local-only trap: builtin BeefAPI preset is injected with enabled=true + apiKey=""
// (backend/internal/workspace/provider_state.go presetChannelMap). For a purely local
// setup that channel must be reported as NOT credential-ready, so the canvas can
// refuse it with an actionable message instead of shipping a doomed upstream request.

test("builtin BeefAPI preset without credentials is NOT credential-ready", () => {
    const channel = {
        id: "beefapi",
        pinned: true,
        baseUrl: "https://enterprise.beefapi.com",
        apiKey: "",
    };
    expect(channelHasManagedBeefAPICredential(channel)).toBe(false);
    expect(channelHasGenerationCredential(channel)).toBe(false);
});

test("local channels are credential-ready with no API key", () => {
    for (const baseUrl of ["http://localhost:8198", "http://127.0.0.1:8198", "http://localhost:11434"]) {
        const channel = { id: "x", pinned: false, baseUrl, apiKey: "" };
        expect(channelHasGenerationCredential(channel)).toBe(true);
    }
});

test("non-local remote channel with a key IS credential-ready", () => {
    const channel = { id: "x", pinned: false, baseUrl: "https://api.example.com", apiKey: "sk-test" };
    expect(channelHasGenerationCredential(channel)).toBe(true);
});

test("seedance contract only applies to the hosted BeefAPI endpoint", () => {
    // A local proxy must never inherit inline-media Seedance semantics from the
    // hosted preset, otherwise local video refs get base64-inlined for no reason.
    expect(beefAPIVideoContract("seedance-2.0")).toBeDefined();
    expect(isBuiltinBeefAPIChannel({ id: "beefapi", baseUrl: "http://localhost:8198" } as never)).toBe(false);
});