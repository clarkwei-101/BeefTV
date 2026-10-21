import { apiClient } from "@/services/api/request";

/**
 * Serialises `apiClient.defaults.adapter` swaps across test files.
 *
 * `apiClient.defaults` is a single module-level object shared by every suite in
 * the bun test process. Suites that intercept HTTP swap the adapter:
 *
 *     const previous = apiClient.defaults.adapter;
 *     apiClient.defaults.adapter = adapter;
 *     try { ... } finally { apiClient.defaults.adapter = previous; }
 *
 * When two suites overlap, suite A's `finally` restores the pre-existing adapter
 * while suite B is still running against its own. B's request escapes to the real
 * transport, its mocked response never arrives, and the assertion fails with a
 * shape error such as "素材列表无效". That produced ~42 cross-suite failures that
 * all pass when their file runs alone.
 *
 * The swap window is serialised with a promise chain. The guard is re-entrant:
 * a suite may call this again from inside its own `run` without deadlocking on
 * the lock it already holds.
 */

type MutableDefaults = { adapter?: unknown };

let tail: Promise<void> = Promise.resolve();
let depth = 0;

/**
 * Run `run` while `adapter` is installed, with no other suite's swap overlapping.
 * Replaces the per-file local `withAdapter` helper.
 */
export async function withIsolatedAdapter<T>(adapter: unknown, run: () => Promise<T>): Promise<T> {
    const defaults = apiClient.defaults as MutableDefaults;

    // Re-entrant call from inside a run() this helper already owns: keep the
    // outer adapter in place and just run the body, otherwise the inner call
    // would wait on a lock held by its own caller and time out.
    if (depth > 0) {
        const restoreInner = defaults.adapter;
        defaults.adapter = adapter;
        try {
            return await run();
        } finally {
            defaults.adapter = restoreInner;
        }
    }

    let release: () => void = () => {};
    const next = new Promise<void>((resolve) => {
        release = resolve;
    });
    const previous = tail;
    tail = previous.then(() => next);

    await previous;
    const restore = defaults.adapter;
    defaults.adapter = adapter;
    depth += 1;
    try {
        return await run();
    } finally {
        depth -= 1;
        defaults.adapter = restore;
        release();
    }
}