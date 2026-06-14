import { describe, expect, it } from "vitest";

import { feedTick, getLtp } from "./useLtpStream";

// feedTick / getLtp back the live per-symbol LTP map consumed by
// PositionsTable etc. The module store is shared, so each test uses unique
// symbol names and the eviction test feeds > the cap to flush prior entries.

describe("useLtpStream store", () => {
  it("stores and retrieves an LTP", () => {
    feedTick("STORE_A", 123.45);
    expect(getLtp("STORE_A")).toBe(123.45);
  });

  it("overwrites an existing symbol's value", () => {
    feedTick("STORE_B", 100);
    feedTick("STORE_B", 200);
    expect(getLtp("STORE_B")).toBe(200);
  });

  it("ignores invalid inputs (empty symbol, NaN, zero, negative)", () => {
    feedTick("", 100);
    expect(getLtp("")).toBeUndefined();
    feedTick("STORE_NAN", Number.NaN);
    expect(getLtp("STORE_NAN")).toBeUndefined();
    feedTick("STORE_ZERO", 0);
    expect(getLtp("STORE_ZERO")).toBeUndefined();
    feedTick("STORE_NEG", -5);
    expect(getLtp("STORE_NEG")).toBeUndefined();
  });

  it("evicts the oldest symbols beyond the 300-symbol cap (LRU)", () => {
    // Feed > MAX_TRACKED_SYMBOLS so the most-recent 300 survive regardless of
    // entries left by earlier tests.
    const N = 310;
    for (let i = 0; i < N; i++) feedTick(`LRU${i}`, i + 1);
    expect(getLtp("LRU309")).toBe(310); // newest survives
    expect(getLtp("LRU0")).toBeUndefined(); // oldest evicted
    expect(getLtp("LRU9")).toBeUndefined(); // boundary: just evicted
    expect(getLtp("LRU10")).toBe(11); // boundary: just survives
  });
});
