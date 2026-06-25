import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { renderHook, act } from "@testing-library/react";
import { AuthProvider, useAuth } from "./useAuth";

function loginResponse(status: number, body: unknown) {
  return vi.fn().mockResolvedValue({
    ok: status >= 200 && status < 300,
    status,
    json: async () => body,
  });
}

function render() {
  return renderHook(() => useAuth(), { wrapper: AuthProvider });
}

describe("useAuth", () => {
  beforeEach(() => {
    localStorage.clear();
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("starts unauthenticated with empty storage", () => {
    const { result } = render();
    expect(result.current.isAuthenticated).toBe(false);
    expect(result.current.authHeader).toBeNull();
  });

  it("inherits a token from localStorage on mount (fresh tab)", () => {
    localStorage.setItem("yv_token", "preexisting");
    const { result } = render();
    expect(result.current.isAuthenticated).toBe(true);
    expect(result.current.authHeader).toBe("Bearer preexisting");
  });

  it("login success stores token + csrf, authenticates, and purges legacy password", async () => {
    vi.stubGlobal("fetch", loginResponse(200, { token: "tok1", csrf_token: "csrf1" }));
    localStorage.setItem("yv_password", "legacy"); // older builds persisted this
    const { result } = render();

    let ok: boolean | undefined;
    await act(async () => {
      ok = await result.current.login("pw");
    });

    expect(ok).toBe(true);
    expect(result.current.isAuthenticated).toBe(true);
    expect(result.current.authHeader).toBe("Bearer tok1");
    expect(result.current.csrfToken).toBe("csrf1");
    expect(localStorage.getItem("yv_token")).toBe("tok1");
    expect(localStorage.getItem("yv_password")).toBeNull();
  });

  it("login with wrong password (401) returns false and stays unauthenticated", async () => {
    vi.stubGlobal("fetch", loginResponse(401, {}));
    const { result } = render();

    let ok: boolean | undefined;
    await act(async () => {
      ok = await result.current.login("bad");
    });

    expect(ok).toBe(false);
    expect(result.current.isAuthenticated).toBe(false);
  });

  it("login throttled (429) throws a rate-limit error", async () => {
    vi.stubGlobal("fetch", loginResponse(429, {}));
    const { result } = render();
    await expect(result.current.login("pw")).rejects.toThrow(/too many/i);
  });

  it("login network failure throws 'Cannot connect to server'", async () => {
    vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new TypeError("down")));
    const { result } = render();
    await expect(result.current.login("pw")).rejects.toThrow("Cannot connect to server");
  });

  it("logout clears state and storage", async () => {
    vi.stubGlobal("fetch", loginResponse(200, { token: "tok1", csrf_token: "csrf1" }));
    const { result } = render();
    await act(async () => {
      await result.current.login("pw");
    });

    act(() => result.current.logout());

    expect(result.current.isAuthenticated).toBe(false);
    expect(result.current.authHeader).toBeNull();
    expect(localStorage.getItem("yv_token")).toBeNull();
    expect(localStorage.getItem("yv_csrf")).toBeNull();
  });

  it("syncs auth across tabs via the storage event", () => {
    const { result } = render();
    expect(result.current.isAuthenticated).toBe(false);

    act(() => {
      localStorage.setItem("yv_token", "tok-from-other-tab");
      window.dispatchEvent(
        new StorageEvent("storage", { key: "yv_token", newValue: "tok-from-other-tab" })
      );
    });

    expect(result.current.isAuthenticated).toBe(true);
    expect(result.current.authHeader).toBe("Bearer tok-from-other-tab");
  });
});
