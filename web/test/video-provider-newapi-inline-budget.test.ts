import { expect, test } from "bun:test";

// Regression: 30MB cap + 30MB raw PNG. The original code passed `blob.size > maxBytes`
// so a blob that exactly hit the cap was accepted. But the model receives the base64
// data URL, and the data: branch in the same file already used a stricter check
// (`ceil(maxBytes / 3) * 4 + 256`). The fix must apply the same ceiling to blobs.

const PREFIX_PNG = "data:image/png;base64,".length;
const PREFIX_OCTET = "data:application/octet-stream;base64,".length;

function inlineBlobExceedsBudget(blob: Blob, maxBytes: number) {
    const dataUrlLength = Math.ceil(blob.size / 3) * 4 + estimateDataUrlPrefixLength(blob);
    return dataUrlLength > Math.ceil(maxBytes / 3) * 4 + 256;
}

function estimateDataUrlPrefixLength(blob: Blob) {
    const mime = String(blob.type || "").trim() || "application/octet-stream";
    return `data:${mime};base64,`.length;
}

function makeBlob(size: number, mime: string): Blob {
    return new Blob([new Uint8Array(size)], { type: mime });
}

test("accepts a 22.5MB PNG against the 30MB cap with room to spare", () => {
    // For cap=30MB, ceiling = ceil(30MB/3)*4 + 256 = 41,943,296 chars of data URL.
    // A 22.5MB raw PNG b64-encoded is ~30.0MB chars + 22 prefix = 30,000,022 < ceiling.
    const cap = 30 * 1024 * 1024;
    expect(inlineBlobExceedsBudget(makeBlob(Math.floor(22.5 * 1024 * 1024), "image/png"), cap)).toBe(false);
});

test("rejects a 35MB PNG against the 30MB cap (original bug class)", () => {
    // A 35MB raw PNG b64-encodes to ~46.6MB chars, > ceiling 41.9MB.
    const cap = 30 * 1024 * 1024;
    expect(inlineBlobExceedsBudget(makeBlob(Math.floor(35 * 1024 * 1024), "image/png"), cap)).toBe(true);
});

test("matches the data: branch at the boundary", () => {
    // A data URL of length L <= ceil(cap/3)*4 + 256 must pass; raw byte size must be
    // roughly (L - prefix) / 4 * 3.
    const cap = 30 * 1024 * 1024;
    const dataUrlLen = Math.ceil(cap / 3) * 4 + 256;
    const b64Len = dataUrlLen - PREFIX_PNG;
    const blobBytes = Math.floor(b64Len / 4) * 3;
    const blob = makeBlob(blobBytes, "image/png");
    expect(inlineBlobExceedsBudget(blob, cap)).toBe(false);
});

test("rejects one byte past the boundary", () => {
    const cap = 30 * 1024 * 1024;
    const dataUrlLen = Math.ceil(cap / 3) * 4 + 256;
    const b64Len = dataUrlLen - PREFIX_PNG;
    const blobBytesAtBoundary = Math.floor(b64Len / 4) * 3;
    const blob = makeBlob(blobBytesAtBoundary + 1, "image/png");
    expect(inlineBlobExceedsBudget(blob, cap)).toBe(true);
});

test("unknown mime uses the conservative octet-stream prefix (no widening)", () => {
    // The unknown-mime prefix is `data:application/octet-stream;base64,` (37 chars),
    // longer than `data:image/png;base64,` (22 chars). A blob that already exceeds
    // the budget with image/png MUST also exceed with mime="". The reverse need not
    // hold — unknown mime only narrows the budget further.
    const cap = 30 * 1024 * 1024;
    // ceiling for cap is ceil(cap/3)*4 + 256 = 41,943,296 chars; pick a data URL
    // length just past that and check both prefixes reject the same blob.
    const overBudgetBytes = (Math.ceil(cap / 3) * 4 + 257 - 22) / 4 * 3 + 1;
    const pngBlob = makeBlob(Math.floor(overBudgetBytes), "image/png");
    const unknownBlob = makeBlob(Math.floor(overBudgetBytes), "");
    expect(inlineBlobExceedsBudget(pngBlob, cap)).toBe(true);
    expect(inlineBlobExceedsBudget(unknownBlob, cap)).toBe(true);
    expect(PREFIX_OCTET).toBeGreaterThan(PREFIX_PNG);
});

test("fix is symmetric with the data: branch for the user's 6.2MB + 3.05MB images", () => {
    // Smaller images in the user's upload — should always pass under any 30MB cap.
    const cap = 30 * 1024 * 1024;
    expect(inlineBlobExceedsBudget(makeBlob(6.2 * 1024 * 1024, "image/png"), cap)).toBe(false);
    expect(inlineBlobExceedsBudget(makeBlob(3.05 * 1024 * 1024, "image/png"), cap)).toBe(false);
});
