import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import {
  apiFetch,
  setAuthHeader,
  setCsrfToken,
  setOnUnauthorized,
} from "./client";

function fetchMock(
  status: number,
  body: unknown,
  opts: { ok?: boolean; rejectJson?: boolean } = {}
) {
  return vi.fn().mockResolvedValue({
    status,
    ok: opts.ok ?? (status >= 200 && status < 300),
    statusText: "STATUS",
    json: async () => {
      if (opts.rejectJson) throw new Error("not json");
      return body;
    },
  });
}

function lastInit(fn: ReturnType<typeof vi.fn>): RequestInit {
  return fn.mock.calls[0][1] as RequestInit;
}

function headersOf(fn: ReturnType<typeof vi.fn>): Record<string, string> {
  return (lastInit(fn).headers ?? {}) as Record<string, string>;
}

describe("apiFetch", () => {
  beforeEach(() => {
    setAuthHeader(null);
    setCsrfToken(null);
    setOnUnauthorized(() => {});
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("returns parsed JSON on success", async () => {
    vi.stubGlobal("fetch", fetchMock(200, { hello: "world" }));
    await expect(apiFetch("/api/x")).resolves.toEqual({ hello: "world" });
  });

  it("attaches the Authorization header when set", async () => {
    const f = fetchMock(200, {});
    vi.stubGlobal("fetch", f);
    setAuthHeader("Bearer abc");
    await apiFetch("/api/x");
    expect(headersOf(f)["Authorization"]).toBe("Bearer abc");
  });

  it("adds X-CSRF-Token only on state-changing methods", async () => {
    const f = fetchMock(200, {});
    vi.stubGlobal("fetch", f);
    setCsrfToken("csrf123");
    await apiFetch("/api/x", { method: "POST" });
    expect(headersOf(f)["X-CSRF-Token"]).toBe("csrf123");
  });

  it("does NOT add X-CSRF-Token on GET", async () => {
    const f = fetchMock(200, {});
    vi.stubGlobal("fetch", f);
    setCsrfToken("csrf123");
    await apiFetch("/api/x");
    expect(headersOf(f)["X-CSRF-Token"]).toBeUndefined();
  });

  it("calls onUnauthorized and throws on 401", async () => {
    vi.stubGlobal("fetch", fetchMock(401, {}));
    const onUnauth = vi.fn();
    setOnUnauthorized(onUnauth);
    await expect(apiFetch("/api/x")).rejects.toThrow("Unauthorized");
    expect(onUnauth).toHaveBeenCalledOnce();
  });

  it("throws an Error carrying status + string detail on a 4xx", async () => {
    vi.stubGlobal("fetch", fetchMock(400, { detail: "bad thing" }));
    await expect(apiFetch("/api/x")).rejects.toMatchObject({
      message: "bad thing",
      status: 400,
      detail: "bad thing",
    });
  });

  it("attaches structured (object) detail so callers can branch", async () => {
    const objDetail = { code: "CDSL_TPIN", message: "auth needed" };
    vi.stubGlobal("fetch", fetchMock(403, { detail: objDetail }));
    await apiFetch("/api/x").then(
      () => expect.fail("should have thrown"),
      (e: Error & { status?: number; detail?: unknown }) => {
        expect(e.status).toBe(403);
        expect(e.detail).toEqual(objDetail);
      }
    );
  });

  it("falls back to a generic message when the error body isn't JSON", async () => {
    vi.stubGlobal("fetch", fetchMock(500, null, { rejectJson: true }));
    await expect(apiFetch("/api/x")).rejects.toMatchObject({ status: 500 });
  });

  it("runs the optional validator on the parsed body and returns its result", async () => {
    vi.stubGlobal("fetch", fetchMock(200, { ok: true }));
    const validate = vi.fn((raw: unknown) => raw as { ok: boolean });
    const data = await apiFetch("/api/x", undefined, validate);
    expect(validate).toHaveBeenCalledWith({ ok: true });
    expect(data).toEqual({ ok: true });
  });

  it("propagates a validator failure (shape drift) as a thrown error", async () => {
    // e.g. an endpoint that should return an array returns an object instead.
    vi.stubGlobal("fetch", fetchMock(200, { not: "an array" }));
    const validate = () => {
      throw new Error("expected an array, got object");
    };
    await expect(apiFetch("/api/x", undefined, validate)).rejects.toThrow(
      /expected an array/
    );
  });
});
