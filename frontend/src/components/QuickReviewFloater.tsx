import { useEffect, useMemo, useRef, useState } from "react";
import { Link } from "react-router-dom";
import clsx from "clsx";
import {
  useAddUserWatchlistSymbol,
  useCreateAlert,
  useManualTrade,
  useRecentTradedSymbols,
  useReviewHoldings,
  useSymbolQuickContext,
  useUniverseSymbols,
} from "../hooks/queries";
import { fmt, fmtCompact } from "../utils/format";

const RECENT_KEY = "quickReview.recent";
const MAX_RECENT = 5;

function loadRecent(): string[] {
  try {
    const raw = localStorage.getItem(RECENT_KEY);
    if (!raw) return [];
    const parsed = JSON.parse(raw);
    return Array.isArray(parsed) ? parsed.filter((s) => typeof s === "string") : [];
  } catch {
    return [];
  }
}

function pushRecent(sym: string): string[] {
  const cur = loadRecent().filter((s) => s !== sym);
  const next = [sym, ...cur].slice(0, MAX_RECENT);
  localStorage.setItem(RECENT_KEY, JSON.stringify(next));
  return next;
}

export function QuickReviewFloater() {
  const [open, setOpen] = useState(false);
  const [input, setInput] = useState("");
  const [activeSym, setActiveSym] = useState<string>("");
  const [showSuggestions, setShowSuggestions] = useState(false);
  const [recent, setRecent] = useState<string[]>(() => loadRecent());
  const inputRef = useRef<HTMLInputElement>(null);

  const { data: allSymbols } = useUniverseSymbols();
  const { data: recentTradedSymbols } = useRecentTradedSymbols(10);
  const quickCtx = useSymbolQuickContext(activeSym);
  const review = useReviewHoldings();
  const addWatchlist = useAddUserWatchlistSymbol();
  const createAlert = useCreateAlert();
  const manualTrade = useManualTrade();
  const [tradePanelOpen, setTradePanelOpen] = useState(false);
  const [tradeQty, setTradeQty] = useState(1);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const isToggle =
        (e.key === "k" || e.key === "K") && (e.metaKey || e.ctrlKey);
      if (isToggle) {
        e.preventDefault();
        setOpen((v) => !v);
        return;
      }
      if (e.key === "Escape" && open) {
        setOpen(false);
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open]);

  useEffect(() => {
    if (open) {
      setTimeout(() => inputRef.current?.focus(), 50);
    }
  }, [open]);

  const suggestions = useMemo(() => {
    if (!input || !allSymbols) return [];
    const q = input.toUpperCase();
    return allSymbols
      .filter((s) => s.toUpperCase().startsWith(q))
      .slice(0, 8);
  }, [input, allSymbols]);

  const runReview = (sym?: string) => {
    const s = (sym ?? input).trim().toUpperCase();
    if (!s) return;
    setActiveSym(s);
    setInput(s);
    setShowSuggestions(false);
    setRecent(pushRecent(s));
    // Reset per-symbol action state so a new review starts clean.
    setTradePanelOpen(false);
    addWatchlist.reset();
    createAlert.reset();
    manualTrade.reset();
    review.mutate([s]);
  };

  // Clear input + active symbol + result so the floater returns to
  // the "type a symbol" baseline. Used by the cross button, by
  // pressing Enter on an empty input, and indirectly when the user
  // selects a different symbol (the new selection overwrites).
  const clearAll = () => {
    setInput("");
    setActiveSym("");
    setShowSuggestions(false);
    review.reset();
    setTradePanelOpen(false);
    addWatchlist.reset();
    createAlert.reset();
    manualTrade.reset();
    inputRef.current?.focus();
  };

  const reco = review.data?.recommendations.find((r) => r.symbol === activeSym);
  const bars = quickCtx.data?.bars ?? [];
  const lastBar = bars[bars.length - 1];
  const prevBar = bars[bars.length - 2];
  const sevenBackBar = bars.length >= 8 ? bars[bars.length - 8] : bars[0];
  const ltp = quickCtx.data?.ltp ?? lastBar?.close ?? null;
  const dayOpen = lastBar?.open ?? null;
  const dayHigh = lastBar?.high ?? null;
  const dayLow = lastBar?.low ?? null;
  const prevClose = prevBar?.close ?? null;
  const dayChange =
    ltp != null && prevClose != null
      ? ((ltp - prevClose) / prevClose) * 100
      : null;
  const sevenDayChange =
    ltp != null && sevenBackBar?.close
      ? ((ltp - sevenBackBar.close) / sevenBackBar.close) * 100
      : null;

  return (
    <>
      <button
        type="button"
        onClick={() => setOpen(true)}
        className="fixed bottom-6 right-6 z-40 h-12 w-12 sm:h-14 sm:w-14 rounded-full bg-emerald-600 hover:bg-emerald-500 text-white shadow-lg shadow-emerald-900/40 transition-colors flex items-center justify-center"
        title="Quick ML Review (⌘K / Ctrl+K)"
        aria-label="Open Quick ML Review"
      >
        <svg
          xmlns="http://www.w3.org/2000/svg"
          viewBox="0 0 24 24"
          fill="none"
          stroke="currentColor"
          strokeWidth="2"
          className="h-6 w-6 sm:h-7 sm:w-7"
        >
          <path d="M13 2L3 14h7l-1 8 10-12h-7l1-8z" strokeLinejoin="round" strokeLinecap="round" />
        </svg>
      </button>

      {open && (
        <div
          className="fixed inset-0 z-50 bg-black/60 flex items-start sm:items-center justify-center p-2 sm:p-6"
          // Dismiss only when the mouse press STARTED on the backdrop
          // itself. Using onClick triggered the close when the user
          // dragged a text selection from inside the popup out into the
          // backdrop — the resulting click event targets the backdrop
          // even though the gesture started on content. onMouseDown +
          // identity check on currentTarget catches "started outside"
          // intent without firing on "drag-released outside".
          onMouseDown={(e) => {
            if (e.target === e.currentTarget) setOpen(false);
          }}
        >
          <div
            className="bg-gray-900 border border-gray-700 rounded-xl shadow-2xl w-full max-w-3xl min-h-[60vh] max-h-[90vh] sm:my-10 overflow-hidden flex flex-col"
          >
            <div className="flex items-center justify-between px-4 py-3 border-b border-gray-800">
              <div className="flex items-center gap-2">
                <span className="text-emerald-400">⚡</span>
                <h3 className="text-sm font-semibold text-gray-100">Quick ML Review</h3>
                <kbd className="hidden sm:inline-block text-[10px] text-gray-500 border border-gray-700 rounded px-1.5 py-0.5">
                  ⌘K
                </kbd>
              </div>
              <button
                type="button"
                onClick={() => setOpen(false)}
                className="text-gray-500 hover:text-gray-300 text-xl leading-none"
                aria-label="Close"
              >
                ×
              </button>
            </div>

            <div className="p-4 space-y-3 overflow-y-auto">
              <div className="relative">
                <input
                  ref={inputRef}
                  type="text"
                  value={input}
                  onChange={(e) => {
                    setInput(e.target.value.toUpperCase());
                    setShowSuggestions(true);
                  }}
                  onFocus={() => setShowSuggestions(true)}
                  onBlur={() => setTimeout(() => setShowSuggestions(false), 150)}
                  onKeyDown={(e) => {
                    if (e.key === "Enter") {
                      // Enter on an empty input is the keyboard
                      // shortcut for clear — equivalent to clicking
                      // the × button. Otherwise: pick the top
                      // suggestion when one is visible, else run
                      // whatever's typed.
                      if (!input.trim()) {
                        clearAll();
                      } else if (suggestions.length > 0 && showSuggestions) {
                        runReview(suggestions[0]);
                      } else {
                        runReview();
                      }
                    }
                  }}
                  placeholder="Type a symbol (e.g. RELIANCE, TCS)…"
                  className="w-full bg-gray-800 border border-gray-700 rounded-lg pl-3 pr-9 py-2 text-sm text-gray-100 focus:outline-none focus:border-emerald-500"
                />
                {(input || activeSym) && (
                  <button
                    type="button"
                    onClick={clearAll}
                    aria-label="Clear input and result"
                    title="Clear (Enter on empty input also works)"
                    className="absolute right-2 top-1/2 -translate-y-1/2 w-6 h-6 flex items-center justify-center text-gray-500 hover:text-gray-200 hover:bg-gray-700 rounded transition-colors text-lg leading-none"
                  >
                    ×
                  </button>
                )}
                {showSuggestions && suggestions.length > 0 && (
                  <div className="absolute z-10 mt-1 w-full bg-gray-800 border border-gray-700 rounded-lg shadow-xl max-h-56 overflow-y-auto">
                    {suggestions.map((s) => (
                      <button
                        type="button"
                        key={s}
                        onMouseDown={() => runReview(s)}
                        className="block w-full text-left px-3 py-1.5 text-sm text-gray-200 hover:bg-gray-700"
                      >
                        {s}
                      </button>
                    ))}
                  </div>
                )}
              </div>

              {!activeSym && (
                <div className="space-y-3">
                  {recent.length > 0 && (
                    <div>
                      <div className="text-[10px] uppercase tracking-wide text-gray-500 mb-1">
                        Recent reviews
                      </div>
                      <div className="flex flex-wrap gap-1.5">
                        {recent.map((s) => (
                          <button
                            type="button"
                            key={s}
                            onClick={() => runReview(s)}
                            className="text-xs bg-gray-800 hover:bg-gray-700 text-gray-300 px-2 py-1 rounded"
                          >
                            {s}
                          </button>
                        ))}
                      </div>
                    </div>
                  )}

                  {(recentTradedSymbols ?? []).length > 0 && (
                    <div>
                      <div className="text-[10px] uppercase tracking-wide text-gray-500 mb-1">
                        From your trades
                      </div>
                      <div className="flex flex-wrap gap-1.5">
                        {(recentTradedSymbols ?? []).map((s) => (
                          <button
                            type="button"
                            key={s}
                            onClick={() => runReview(s)}
                            className="text-xs bg-emerald-900/30 hover:bg-emerald-900/50 text-emerald-300 px-2 py-1 rounded border border-emerald-900/40"
                          >
                            {s}
                          </button>
                        ))}
                      </div>
                    </div>
                  )}

                  {recent.length === 0 &&
                    (recentTradedSymbols ?? []).length === 0 && (
                      <p className="text-xs text-gray-500 py-4 text-center">
                        Type a symbol above to start. Press{" "}
                        <kbd className="border border-gray-700 rounded px-1">⌘K</kbd>{" "}
                        any time to reopen.
                      </p>
                    )}
                </div>
              )}

              {activeSym && (
                <div className="space-y-3">
                  <div className="bg-gray-800/50 border border-gray-700 rounded-lg p-3">
                    <div className="flex items-baseline justify-between gap-2 mb-2">
                      <div className="flex items-baseline gap-2">
                        <Link
                          to={`/symbol/${activeSym}`}
                          onClick={() => setOpen(false)}
                          className="text-base font-semibold text-emerald-400 hover:underline"
                        >
                          {activeSym}
                        </Link>
                        {quickCtx.data?.sector && (
                          <span className="text-xs text-gray-500">
                            {quickCtx.data.sector}
                          </span>
                        )}
                        {allSymbols && !allSymbols.includes(activeSym) && (
                          <span
                            className="text-[10px] px-1.5 py-0.5 rounded bg-amber-900/30 text-amber-300/90"
                            title="Not in your tracked universe — the model is running on data fetched on demand, so its confidence is less calibrated for this name."
                          >
                            outside universe
                          </span>
                        )}
                      </div>
                      <div className="text-right">
                        <div className="text-base font-mono text-gray-100">
                          ₹{fmt(ltp)}
                        </div>
                        {dayChange != null && (
                          <div
                            className={clsx(
                              "text-xs",
                              dayChange > 0
                                ? "text-emerald-400"
                                : dayChange < 0
                                  ? "text-red-400"
                                  : "text-gray-400",
                            )}
                          >
                            {dayChange >= 0 ? "+" : ""}
                            {dayChange.toFixed(2)}%
                          </div>
                        )}
                      </div>
                    </div>

                    <div className="grid grid-cols-2 sm:grid-cols-4 gap-2 text-xs">
                      <div>
                        <div className="text-gray-500">Open</div>
                        <div className="text-gray-200 font-mono">₹{fmt(dayOpen)}</div>
                      </div>
                      <div>
                        <div className="text-gray-500">Prev Close</div>
                        <div className="text-gray-200 font-mono">₹{fmt(prevClose)}</div>
                      </div>
                      <div>
                        <div className="text-gray-500">Day Range</div>
                        <div className="text-gray-200 font-mono">
                          ₹{fmt(dayLow)} – ₹{fmt(dayHigh)}
                        </div>
                      </div>
                      <div>
                        <div className="text-gray-500">7d</div>
                        <div
                          className={clsx(
                            "font-mono",
                            sevenDayChange == null
                              ? "text-gray-400"
                              : sevenDayChange > 0
                                ? "text-emerald-400"
                                : sevenDayChange < 0
                                  ? "text-red-400"
                                  : "text-gray-400",
                          )}
                        >
                          {sevenDayChange == null
                            ? "—"
                            : `${sevenDayChange >= 0 ? "+" : ""}${sevenDayChange.toFixed(2)}%`}
                        </div>
                      </div>
                      <div>
                        <div className="text-gray-500">Avg Vol (20d)</div>
                        <div className="text-gray-200 font-mono">
                          {fmtCompact(quickCtx.data?.avg_volume_20d)}
                        </div>
                      </div>
                      <div>
                        <div className="text-gray-500">Today's Vol</div>
                        <div className="text-gray-200 font-mono">
                          {fmtCompact(lastBar?.volume)}
                        </div>
                      </div>
                    </div>

                    {(quickCtx.data?.quarantine.is_quarantined ||
                      quickCtx.data?.is_locked ||
                      quickCtx.data?.open_position ||
                      quickCtx.data?.todays_signal) && (
                      <div className="mt-3 flex flex-wrap gap-1.5">
                        {quickCtx.data?.quarantine.is_quarantined && (
                          <span
                            className="text-[10px] px-2 py-0.5 rounded bg-rose-900/40 text-rose-300"
                            title={quickCtx.data.quarantine.reason ?? "Quarantined"}
                          >
                            QUARANTINED
                          </span>
                        )}
                        {quickCtx.data?.is_locked && (
                          <span className="text-[10px] px-2 py-0.5 rounded bg-amber-900/40 text-amber-300">
                            LOCKED HOLDING
                          </span>
                        )}
                        {quickCtx.data?.open_position && (
                          <span className="text-[10px] px-2 py-0.5 rounded bg-blue-900/40 text-blue-300">
                            HOLDING {quickCtx.data.open_position.signal_type} ×
                            {quickCtx.data.open_position.quantity} @ ₹
                            {fmt(
                              quickCtx.data.open_position.fill_price ??
                                quickCtx.data.open_position.entry_price,
                            )}
                          </span>
                        )}
                        {quickCtx.data?.todays_signal && (
                          <span
                            className="text-[10px] px-2 py-0.5 rounded bg-gray-700 text-gray-300"
                            title={
                              quickCtx.data.todays_signal.disposition_reason ??
                              undefined
                            }
                          >
                            TODAY: {quickCtx.data.todays_signal.signal_type}{" "}
                            {((quickCtx.data.todays_signal.confidence_score ?? 0) * 100).toFixed(0)}%{" "}
                            →{" "}
                            {quickCtx.data.todays_signal.disposition ?? "pending"}
                          </span>
                        )}
                      </div>
                    )}
                  </div>

                  <div>
                    {review.isPending && (
                      <div className="text-sm text-gray-500 py-2">
                        {allSymbols && !allSymbols.includes(activeSym)
                          ? `Fetching live data for ${activeSym}… (not in your tracked universe — this can take a few seconds)`
                          : "Reviewing…"}
                      </div>
                    )}
                    {review.isError && (
                      <div className="text-sm text-red-400 py-2">
                        Review failed:{" "}
                        {review.error instanceof Error
                          ? review.error.message
                          : "unknown error"}
                      </div>
                    )}
                    {!review.isPending && reco && (
                      <div className="bg-gray-800/50 border border-gray-700 rounded-lg p-3">
                        <div className="flex items-center gap-2 mb-2">
                          <span
                            className={clsx("px-2 py-0.5 rounded text-xs font-semibold", {
                              "bg-red-900/40 text-red-400":
                                reco.action === "SELL" || reco.action === "SHORT",
                              "bg-emerald-900/40 text-emerald-400":
                                reco.action === "BUY" || reco.action === "BUY_MORE",
                              "bg-amber-900/40 text-amber-400":
                                reco.action === "TIGHTEN_SL",
                              "bg-gray-700 text-gray-300": reco.action === "HOLD",
                            })}
                          >
                            {reco.action.replace("_", " ")}
                          </span>
                          <span className="text-xs text-gray-400">
                            {(reco.confidence * 100).toFixed(0)}% confidence
                          </span>
                          {reco.target_price != null && (
                            <span className="text-xs text-gray-500">
                              · target ₹{fmt(reco.target_price)}
                            </span>
                          )}
                          {reco.stop_loss_price != null && (
                            <span className="text-xs text-gray-500">
                              · SL ₹{fmt(reco.stop_loss_price)}
                            </span>
                          )}
                        </div>
                        <div className="text-xs text-gray-300 leading-snug">
                          {reco.reasoning}
                        </div>
                        {/* Act on the review: Watch + Alert are one-tap;
                            Trade opens a confirm panel — it places a LIVE
                            order, so it's never a silent one-tap. */}
                        <div className="mt-3 flex flex-wrap items-center gap-2">
                          <button
                            type="button"
                            onClick={() => addWatchlist.mutate({ symbol: activeSym })}
                            disabled={addWatchlist.isPending || addWatchlist.isSuccess}
                            className="text-xs px-2 py-1 rounded border border-gray-600 text-gray-300 hover:bg-gray-700 disabled:opacity-60"
                          >
                            {addWatchlist.isSuccess ? "★ Watching" : "☆ Watch"}
                          </button>
                          <button
                            type="button"
                            onClick={() => {
                              const tp = reco.target_price ?? ltp ?? 0;
                              if (!tp) return;
                              const direction = tp >= (ltp ?? tp) ? "above" : "below";
                              createAlert.mutate({ symbol: activeSym, target_price: tp, direction });
                            }}
                            disabled={
                              createAlert.isPending || createAlert.isSuccess || !(reco.target_price ?? ltp)
                            }
                            title={
                              reco.target_price != null
                                ? `Alert at target ₹${fmt(reco.target_price)}`
                                : "Alert at the current price"
                            }
                            className="text-xs px-2 py-1 rounded border border-gray-600 text-gray-300 hover:bg-gray-700 disabled:opacity-60"
                          >
                            {createAlert.isSuccess ? "🔔 Alert set" : "🔔 Alert"}
                          </button>
                          {(reco.signal_type === "BUY" || reco.signal_type === "SELL") &&
                            reco.target_price != null &&
                            reco.stop_loss_price != null && (
                              <button
                                type="button"
                                onClick={() => setTradePanelOpen((v) => !v)}
                                className="text-xs px-2 py-1 rounded border border-amber-700/60 text-amber-300 hover:bg-amber-900/30"
                              >
                                Trade…
                              </button>
                            )}
                        </div>
                        {tradePanelOpen &&
                          reco.target_price != null &&
                          reco.stop_loss_price != null && (
                            <div className="mt-2 bg-gray-900 border border-amber-800/50 rounded-lg p-3 space-y-2">
                              <div className="text-xs text-amber-300">
                                Place a <b>LIVE {reco.signal_type}</b> on {activeSym} — entry ₹
                                {fmt(ltp)}, target ₹{fmt(reco.target_price)}, SL ₹
                                {fmt(reco.stop_loss_price)}
                              </div>
                              <div className="flex items-center gap-2">
                                <label className="text-xs text-gray-400">Qty</label>
                                <input
                                  type="number"
                                  min={1}
                                  value={tradeQty}
                                  onChange={(e) =>
                                    setTradeQty(Math.max(1, Number(e.target.value) || 1))
                                  }
                                  className="w-20 bg-gray-800 border border-gray-700 rounded px-2 py-1 text-xs text-gray-100"
                                />
                                <button
                                  type="button"
                                  onClick={() =>
                                    manualTrade.mutate({
                                      symbol: activeSym,
                                      signal_type: reco.signal_type,
                                      entry_price: ltp ?? 0,
                                      target_price: reco.target_price as number,
                                      stop_loss_price: reco.stop_loss_price as number,
                                      position_size: tradeQty,
                                    })
                                  }
                                  disabled={
                                    manualTrade.isPending ||
                                    (manualTrade.isSuccess && !!manualTrade.data?.success)
                                  }
                                  className="text-xs px-2.5 py-1 rounded bg-amber-600 hover:bg-amber-700 text-white disabled:opacity-50"
                                >
                                  {manualTrade.isPending ? "Placing…" : "Place live trade"}
                                </button>
                                <button
                                  type="button"
                                  onClick={() => setTradePanelOpen(false)}
                                  className="text-xs text-gray-500 hover:text-gray-300"
                                >
                                  Cancel
                                </button>
                              </div>
                              {manualTrade.isSuccess && (
                                <div
                                  className={clsx(
                                    "text-xs",
                                    manualTrade.data?.success ? "text-emerald-400" : "text-red-400",
                                  )}
                                >
                                  {manualTrade.data?.success
                                    ? "Trade placed."
                                    : `Failed: ${manualTrade.data?.error ?? "unknown error"}`}
                                </div>
                              )}
                              {manualTrade.isError && (
                                <div className="text-xs text-red-400">Failed to place trade.</div>
                              )}
                            </div>
                          )}
                      </div>
                    )}
                    {!review.isPending && review.data && !reco && (
                      <div className="text-sm text-gray-500 py-2">
                        No recommendation returned for {activeSym}.
                      </div>
                    )}
                  </div>
                </div>
              )}
            </div>
          </div>
        </div>
      )}
    </>
  );
}
