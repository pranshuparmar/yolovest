import { describe, expect, it } from "vitest";

import { formatPriceMovePct, priceMovePct } from "./priceMove";

describe("priceMovePct (direction-aware move from entry → destination)", () => {
  it("BUY: destination above entry is positive (favorable)", () => {
    expect(priceMovePct(100, 110, "BUY")).toBeCloseTo(10);
  });
  it("BUY: destination below entry is negative (e.g. a stop-loss)", () => {
    expect(priceMovePct(100, 95, "BUY")).toBeCloseTo(-5);
  });
  it("SELL: destination below entry is positive (favorable for a short)", () => {
    expect(priceMovePct(100, 90, "SELL")).toBeCloseTo(10);
  });
  it("SELL: destination above entry is negative (e.g. a stop-loss)", () => {
    expect(priceMovePct(100, 105, "SELL")).toBeCloseTo(-5);
  });
  it("returns null when either price is missing or zero", () => {
    expect(priceMovePct(null, 100, "BUY")).toBeNull();
    expect(priceMovePct(100, null, "BUY")).toBeNull();
    expect(priceMovePct(undefined, undefined, "BUY")).toBeNull();
    expect(priceMovePct(0, 100, "BUY")).toBeNull();
    expect(priceMovePct(100, 0, "BUY")).toBeNull();
  });
  it("treats any non-SELL direction (incl. undefined) as long", () => {
    expect(priceMovePct(100, 110, undefined)).toBeCloseTo(10);
    expect(priceMovePct(100, 110, "BUY")).toBeCloseTo(10);
  });
});

describe("formatPriceMovePct", () => {
  it("prefixes a + for non-negative values, fixed to 2 digits", () => {
    expect(formatPriceMovePct(10.234)).toBe("+10.23%");
    expect(formatPriceMovePct(0)).toBe("+0.00%");
  });
  it("keeps the minus sign for negatives", () => {
    expect(formatPriceMovePct(-5.1)).toBe("-5.10%");
  });
  it("honours the digits argument", () => {
    expect(formatPriceMovePct(10.2, 1)).toBe("+10.2%");
  });
  it("renders an em-dash for null / non-finite input", () => {
    expect(formatPriceMovePct(null)).toBe("—");
    expect(formatPriceMovePct(Infinity)).toBe("—");
    expect(formatPriceMovePct(NaN)).toBe("—");
  });
});
