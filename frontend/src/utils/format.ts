/** Shared number formatting (Indian locale).
 *
 * Single source of truth for the `fmt` helper that was previously
 * duplicated across PositionsTable / TradesTable / PendingTradesBanner /
 * QuickReviewFloater.
 */

export function fmt(n: number | null | undefined, d = 2): string {
  if (n == null || !Number.isFinite(n)) return "—";
  return n.toLocaleString("en-IN", {
    minimumFractionDigits: d,
    maximumFractionDigits: d,
  });
}

/** Compact Indian notation: 1.25Cr / 3.40L / 8.2K. */
export function fmtCompact(n: number | null | undefined): string {
  if (n == null || !Number.isFinite(n)) return "—";
  if (n >= 1e7) return `${(n / 1e7).toFixed(2)}Cr`;
  if (n >= 1e5) return `${(n / 1e5).toFixed(2)}L`;
  if (n >= 1e3) return `${(n / 1e3).toFixed(1)}K`;
  return n.toFixed(0);
}
