import { useEffect, useReducer } from "react";

// Per-symbol live LTP map fed by the dashboard WebSocket's `tick_update`
// frames. NotificationCenter calls `feedTick` when a frame arrives; the
// `useLtpStream` hook returns the current map and re-renders subscribers
// when any tick lands. Backend throttles broadcasts to ≤1 per symbol per
// second, so even with 20 open positions the render rate stays sane.

const store: Map<string, number> = new Map();
const subscribers: Set<() => void> = new Set();

export function feedTick(symbol: string, ltp: number): void {
  if (!symbol || !Number.isFinite(ltp) || ltp <= 0) return;
  store.set(symbol, ltp);
  for (const cb of subscribers) cb();
}

export function getLtp(symbol: string): number | undefined {
  return store.get(symbol);
}

export function useLtpStream(): Map<string, number> {
  // Force a re-render on any tick by toggling a counter — cheap and
  // doesn't require a full Zustand/Redux setup for this single use.
  const [, force] = useReducer((n: number) => n + 1, 0);
  useEffect(() => {
    subscribers.add(force);
    return () => {
      subscribers.delete(force);
    };
  }, []);
  return store;
}
