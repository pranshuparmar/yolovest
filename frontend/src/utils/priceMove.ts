/**
 * Direction-aware percentage move from entry to a destination price.
 *
 * The output sign reflects the trade's perspective:
 *   - BUY  with dest above entry → positive (favorable)
 *   - BUY  with dest below entry → negative (unfavorable, e.g. SL)
 *   - SELL with dest below entry → positive (favorable for a short)
 *   - SELL with dest above entry → negative (unfavorable, e.g. SL)
 *
 * Returns null when either price is missing / zero so the caller can
 * render a placeholder.
 */
export function priceMovePct(
  entry: number | null | undefined,
  dest: number | null | undefined,
  signalType: string | null | undefined,
): number | null {
  if (!entry || !dest || !Number.isFinite(entry) || !Number.isFinite(dest)) {
    return null;
  }
  if (entry === 0) return null;
  const raw = ((dest - entry) / entry) * 100;
  return signalType === "SELL" ? -raw : raw;
}

/**
 * Render a percentage with a leading sign and a fixed digit count.
 * Hyphen-em-dash for null/invalid input so the table cell stays flush.
 */
export function formatPriceMovePct(
  pct: number | null,
  digits = 2,
): string {
  if (pct == null || !Number.isFinite(pct)) return "—";
  const sign = pct >= 0 ? "+" : "";
  return `${sign}${pct.toFixed(digits)}%`;
}
