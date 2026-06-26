import React from "react";
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { renderHook, act } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { useNotifications } from "./NotificationCenter";

// Minimal controllable WebSocket stand-in: records every instance and lets the
// test fire onopen/onclose to drive the reconnect/backoff logic.
class FakeWebSocket {
  static instances: FakeWebSocket[] = [];
  url: string;
  onopen: (() => void) | null = null;
  onclose: (() => void) | null = null;
  onerror: (() => void) | null = null;
  onmessage: ((e: { data: string }) => void) | null = null;
  close = vi.fn();
  constructor(url: string) {
    this.url = url;
    FakeWebSocket.instances.push(this);
  }
}

function wrapper() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return ({ children }: { children: React.ReactNode }) =>
    React.createElement(QueryClientProvider, { client: qc }, children);
}

const latest = () => FakeWebSocket.instances[FakeWebSocket.instances.length - 1];

describe("useNotifications WebSocket", () => {
  beforeEach(() => {
    FakeWebSocket.instances = [];
    localStorage.clear();
    vi.stubGlobal("WebSocket", FakeWebSocket);
    vi.useFakeTimers();
  });

  afterEach(() => {
    vi.useRealTimers();
    vi.unstubAllGlobals();
  });

  it("connects to /ws with the stored token on mount", () => {
    localStorage.setItem("yv_token", "tok1");
    renderHook(() => useNotifications(), { wrapper: wrapper() });
    expect(FakeWebSocket.instances).toHaveLength(1);
    expect(FakeWebSocket.instances[0].url).toContain("/ws?token=tok1");
  });

  it("reconnects after close with exponential backoff that resets on open", () => {
    renderHook(() => useNotifications(), { wrapper: wrapper() });

    // First close → reconnect scheduled at 1000ms.
    act(() => latest().onclose?.());
    expect(FakeWebSocket.instances).toHaveLength(1);
    act(() => vi.advanceTimersByTime(999));
    expect(FakeWebSocket.instances).toHaveLength(1);
    act(() => vi.advanceTimersByTime(1));
    expect(FakeWebSocket.instances).toHaveLength(2);

    // Second close → 2000ms (backoff doubled).
    act(() => latest().onclose?.());
    act(() => vi.advanceTimersByTime(1999));
    expect(FakeWebSocket.instances).toHaveLength(2);
    act(() => vi.advanceTimersByTime(1));
    expect(FakeWebSocket.instances).toHaveLength(3);

    // A successful open resets the backoff → next close waits 1000ms again.
    act(() => latest().onopen?.());
    act(() => latest().onclose?.());
    act(() => vi.advanceTimersByTime(1000));
    expect(FakeWebSocket.instances).toHaveLength(4);
  });

  it("reads a fresh token at reconnect time (re-login picked up)", () => {
    localStorage.setItem("yv_token", "old");
    renderHook(() => useNotifications(), { wrapper: wrapper() });
    expect(latest().url).toContain("token=old");

    localStorage.setItem("yv_token", "new");
    act(() => latest().onclose?.());
    act(() => vi.advanceTimersByTime(1000));

    expect(latest().url).toContain("token=new");
  });

  it("caps the reconnect backoff at 30s", () => {
    renderHook(() => useNotifications(), { wrapper: wrapper() });

    // Walk retries 0..4 (delays 1000,2000,4000,8000,16000), each firing.
    for (const d of [1000, 2000, 4000, 8000, 16000]) {
      act(() => latest().onclose?.());
      act(() => vi.advanceTimersByTime(d));
    }
    const count = FakeWebSocket.instances.length;

    // retry=5 would be 1000*2^5 = 32000, but it's capped to 30000.
    act(() => latest().onclose?.());
    act(() => vi.advanceTimersByTime(29999));
    expect(FakeWebSocket.instances).toHaveLength(count); // not yet
    act(() => vi.advanceTimersByTime(1));
    expect(FakeWebSocket.instances).toHaveLength(count + 1); // fired at 30000
  });
});
