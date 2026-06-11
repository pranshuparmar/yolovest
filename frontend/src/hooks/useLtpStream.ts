import { useEffect, useReducer } from "react";
import { useQuery } from "@tanstack/react-query";
import { api } from "../api/endpoints";

// Per-symbol live LTP map fed by the dashboard WebSocket's `tick_update`
// frames. NotificationCenter calls `feedTick` when a frame arrives; the
// `useLtpStream` hook returns the current map and re-renders subscribers
// when any tick lands. Backend throttles broadcasts to ≤1 per symbol per
// second, so even with 20 open positions the render rate stays sane.

const store: Map<string, number> = new Map();
const subscribers: Set<() => void> = new Set();

// Bound the map so a long-lived tab cycling through many symbols
// (watchlist rotation, symbol pages) can't grow it without limit.
const MAX_TRACKED_SYMBOLS = 300;

export function feedTick(symbol: string, ltp: number): void {
  if (!symbol || !Number.isFinite(ltp) || ltp <= 0) return;
  // Delete-then-set refreshes insertion order, making eviction ~LRU.
  store.delete(symbol);
  store.set(symbol, ltp);
  if (store.size > MAX_TRACKED_SYMBOLS) {
    const oldest = store.keys().next().value;
    if (oldest !== undefined) store.delete(oldest);
  }
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

/** Poll the batched /api/ltp endpoint for an arbitrary symbol set and
 * feed it into the shared LTP store. Used for surfaces that need a
 * "best-effort" LTP for symbols the ticker isn't subscribed to —
 * closed trades, history pages, etc.
 *
 * The store is shared with the ticker stream, so when a tick lands
 * for a polled symbol the polled value is overwritten with the live
 * one on the next ticker frame.
 */
export function useLtpBatch(symbols: string[]): void {
  const key = symbols.slice().sort().join(",");
  useQuery({
    queryKey: ["ltp-batch", key],
    queryFn: async () => {
      if (!symbols.length) return {} as Record<string, number>;
      const data = await api.ltpBatch(symbols);
      for (const [sym, ltp] of Object.entries(data)) {
        feedTick(sym, ltp);
      }
      return data;
    },
    enabled: symbols.length > 0,
    staleTime: 30_000,
    refetchInterval: 30_000,
  });
}
