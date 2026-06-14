import { beforeEach, describe, expect, it } from "vitest";

import { formatIST, getTimezone, initTimezone, parseUTC } from "./datetime";

describe("parseUTC — suffix-less backend timestamps are UTC, not local", () => {
  it("appends Z to a space-separated SQLite timestamp", () => {
    expect(parseUTC("2026-04-02 10:30:00").toISOString()).toBe(
      "2026-04-02T10:30:00.000Z",
    );
  });
  it("honours an explicit Z suffix", () => {
    expect(parseUTC("2026-04-02T10:30:00Z").toISOString()).toBe(
      "2026-04-02T10:30:00.000Z",
    );
  });
  it("honours an explicit numeric offset", () => {
    // +05:30 IST → 05:00 UTC
    expect(parseUTC("2026-04-02T10:30:00+05:30").toISOString()).toBe(
      "2026-04-02T05:00:00.000Z",
    );
  });
  it("returns an invalid Date for empty input", () => {
    expect(Number.isNaN(parseUTC("").getTime())).toBe(true);
  });
});

describe("display timezone", () => {
  beforeEach(() => initTimezone("Asia/Kolkata"));

  it("initTimezone drives getTimezone", () => {
    initTimezone("UTC");
    expect(getTimezone()).toBe("UTC");
    initTimezone("Asia/Kolkata");
    expect(getTimezone()).toBe("Asia/Kolkata");
  });

  it("formatIST renders the UTC instant in the display timezone", () => {
    initTimezone("Asia/Kolkata");
    // 10:30 UTC = 16:00 IST
    const s = formatIST("2026-04-02 10:30:00");
    expect(s).toContain("Apr");
    expect(s).toContain("2026");
    expect(s).toContain("04:00"); // 16:00 in 12-hour en-IN
  });
});
