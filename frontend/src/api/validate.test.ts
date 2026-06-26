import { describe, it, expect } from "vitest";
import { expectObject, expectArray } from "./validate";

describe("expectObject", () => {
  it("passes a plain object through unchanged", () => {
    const o = { total_capital: 100000 };
    expect(expectObject(o)).toBe(o);
  });

  it.each([
    ["null", null],
    ["an array", [1, 2]],
    ["a string", "oops"],
    ["a number", 42],
  ])("throws on %s", (_label, value) => {
    expect(() => expectObject(value)).toThrow(/expected an object/);
  });
});

describe("expectArray", () => {
  it("passes an array through unchanged", () => {
    const a = [{ symbol: "RELIANCE" }];
    expect(expectArray(a)).toBe(a);
  });

  it.each([
    ["null", null],
    ["an object", { 0: "x" }],
    ["a string", "oops"],
  ])("throws on %s", (_label, value) => {
    expect(() => expectArray(value)).toThrow(/expected an array/);
  });
});
