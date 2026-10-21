import { expect, test } from "bun:test";
import { apiClient } from "@/services/api/request";
import { withIsolatedAdapter } from "./helpers/isolated-adapter";

// Before the mutex, overlapping suites clobbered each other's adapter and
// produced ~42 cross-suite failures. These tests assert the mutex actually
// serialises the swap window.

type MutableDefaults = { adapter?: unknown };

test("overlapping adapter swaps do not clobber each other", async () => {
    const defaults = apiClient.defaults as MutableDefaults;
    const pristine = defaults.adapter;
    const events: string[] = [];

    const adapterA = async () => {
        events.push("A-enter");
        await new Promise((resolve) => setTimeout(resolve, 20));
        events.push("A-exit");
        return { data: {}, status: 200, statusText: "OK", headers: {}, config: {} } as never;
    };
    const adapterB = async () => {
        events.push("B-enter");
        await new Promise((resolve) => setTimeout(resolve, 20));
        events.push("B-exit");
        return { data: {}, status: 200, statusText: "OK", headers: {}, config: {} } as never;
    };

    const observed: string[] = [];

    const runA = withIsolatedAdapter(adapterA, async () => {
        observed.push(defaults.adapter === adapterA ? "A-installed" : "A-CLOBBERED");
        await new Promise((resolve) => setTimeout(resolve, 15));
        observed.push(defaults.adapter === adapterA ? "A-still-installed" : "A-CLOBBERED");
    });

    const runB = withIsolatedAdapter(adapterB, async () => {
        observed.push(defaults.adapter === adapterB ? "B-installed" : "B-CLOBBERED");
        await new Promise((resolve) => setTimeout(resolve, 15));
        observed.push(defaults.adapter === adapterB ? "B-still-installed" : "B-CLOBBERED");
    });

    await Promise.all([runA, runB]);

    expect(observed).toEqual(["A-installed", "A-still-installed", "B-installed", "B-still-installed"]);
    expect(defaults.adapter).toBe(pristine);
    expect(events.length).toBe(0);
});

test("adapter is restored even when the run throws", async () => {
    const defaults = apiClient.defaults as MutableDefaults;
    const pristine = defaults.adapter;
    const adapter = async () => ({ data: {}, status: 200, statusText: "OK", headers: {}, config: {} }) as never;

    await expect(
        withIsolatedAdapter(adapter, async () => {
            throw new Error("boom");
        }),
    ).rejects.toThrow("boom");

    expect(defaults.adapter).toBe(pristine);
});