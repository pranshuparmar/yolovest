// Shared news source label + chip styling. Used by both the News Feed
// page (filter chips) and the Symbol detail page (per-headline tag) so
// the rendering stays consistent across the app.

export const NEWS_SOURCE_STYLES: Record<string, { color: string; label: string }> = {
  moneycontrol: { color: "bg-blue-900/40 text-blue-400", label: "MoneyControl" },
  et_markets: { color: "bg-purple-900/40 text-purple-400", label: "ET Markets" },
  livemint: { color: "bg-emerald-900/40 text-emerald-400", label: "LiveMint" },
  nse: { color: "bg-amber-900/40 text-amber-400", label: "NSE Official" },
  google_finance: { color: "bg-red-900/40 text-red-400", label: "Google Finance" },
};

export function newsSourceLabel(source: string): string {
  return NEWS_SOURCE_STYLES[source]?.label ?? source;
}

export function newsSourceColorClass(source: string): string {
  return NEWS_SOURCE_STYLES[source]?.color ?? "bg-gray-800 text-gray-400";
}
