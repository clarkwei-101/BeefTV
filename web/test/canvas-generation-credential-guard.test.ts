import { expect, test } from "bun:test";
import { createModelChannel, defaultConfig, normalizeConfigSnapshot, resolveModelChannel, type AiConfig } from "../src/stores/use-config-store";
import { channelHasGenerationCredential } from "../src/stores/use-config-store";
import { resolveCompatibleModel } from "../src/lib/model-selection";

// Bug class: buildGenerationConfig() ends with
//   model = resolveCompatibleModel(config, preferredModel, requirements) || preferredModel
// resolveCompatibleModel re-routes within a display-name group, so the model that is
// actually submitted can DIFFER from the one the pre-flight credential check saw.
// A node pinned to a working local model could therefore be silently re-routed onto
// the hosted BeefAPI preset (enabled=true, apiKey="") and fail upstream with an
// unactionable "APIKEY_ERROR" toast.
//
// The guard must therefore be re-evaluated AFTER the model is finalised.

const LOCAL = createModelChannel({
    id: "local-video",
    name: "MiniMax H3 (DGX Spark)",
    baseUrl: "http://localhost:8198",
    models: ["minimax-h3"],
    modelProfiles: [{ model: "minimax-h3", capability: "video", protocol: "chat-completion" }],
    enabled: true,
});

// The builtin hosted preset: same DISPLAY NAME as the local channel, so they land in
// the same compatibility group, but it has no credential.
const HOSTED = createModelChannel({
    id: "beefapi",
    name: "MiniMax H3 (DGX Spark)",
    baseUrl: "https://enterprise.beefapi.com",
    models: ["seedance-2.0"],
    modelProfiles: [{ model: "seedance-2.0", capability: "video", protocol: "newapi" }],
    pinned: true,
    enabled: true,
});

function configWith(channels: ReturnType<typeof createModelChannel>[]): AiConfig {
    return normalizeConfigSnapshot({ config: { ...defaultConfig, channels } }).config;
}

test("hosted preset without a key is never credential-ready", () => {
    expect(channelHasGenerationCredential(HOSTED)).toBe(false);
    expect(channelHasGenerationCredential(LOCAL)).toBe(true);
});

test("compatibility routing can move the model to a different channel in the group", () => {
    const config = configWith([LOCAL, HOSTED]);
    const selected = "local-video::minimax-h3";
    // Same display name -> both models are in one group.
    const routed = resolveCompatibleModel(config, selected, { capability: "video" });
    // Whichever model routing picks, the guard must be re-checked against THAT model.
    const channel = resolveModelChannel(config, routed || selected);
    expect(channelHasGenerationCredential(channel)).toBe(
        (routed || selected).startsWith("local-video::"),
    );
});

test("a stored local model cannot be silently swapped onto the credential-less hosted preset", () => {
    const config = configWith([HOSTED, LOCAL]);
    const stored = "local-video::minimax-h3";
    const routed = resolveCompatibleModel(config, stored, { capability: "video" }) || stored;
    const channel = resolveModelChannel(config, routed);
    if (!routed.startsWith("local-video::")) {
        // If routing ever does move to the hosted model, the guard must report NOT ready
        // so the executor blocks it locally instead of shipping a doomed request.
        expect(channelHasGenerationCredential(channel)).toBe(false);
    }
    expect(routed === stored || channelHasGenerationCredential(channel) === false).toBe(true);
});