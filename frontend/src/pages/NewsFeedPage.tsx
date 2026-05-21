import { useState, useMemo, useRef, useCallback, useEffect } from "react";
import { useNewsInfinite, useSentiment } from "../hooks/queries";
import clsx from "clsx";
import { getTimezone } from "../utils/datetime";
import {
  NEWS_SOURCE_STYLES as sourceColors,
  newsSourceLabel as sourceLabel,
  newsSourceColorClass as sourceColorClass,
} from "../utils/newsSource";
import type { NewsArticle } from "../types/api";

function SourceChip({
  source,
  active,
  onClick,
}: {
  source: string;
  active: boolean;
  onClick: () => void;
}) {
  return (
    <button
      onClick={onClick}
      className={clsx(
        "px-2 py-0.5 rounded text-xs font-medium transition-all",
        sourceColorClass(source),
        active && "ring-1 ring-current",
        "cursor-pointer hover:opacity-80"
      )}
    >
      {sourceLabel(source)}
    </button>
  );
}

function SentimentBadge({ symbol }: { symbol: string }) {
  const { data } = useSentiment(symbol);
  if (!data) return null;

  const color =
    data.sentiment === "bullish"
      ? "text-emerald-400"
      : data.sentiment === "bearish"
        ? "text-red-400"
        : "text-gray-400";

  return (
    <span className={clsx("text-xs font-medium", color)}>
      {data.sentiment} ({Math.round(data.confidence * 100)}%)
    </span>
  );
}

function formatDateKey(iso: string): string {
  const d = new Date(iso);
  const today = new Date();
  const yesterday = new Date();
  yesterday.setDate(today.getDate() - 1);

  if (d.toDateString() === today.toDateString()) return "Today";
  if (d.toDateString() === yesterday.toDateString()) return "Yesterday";
  return d.toLocaleDateString("en-IN", {
    timeZone: getTimezone(),
    weekday: "short",
    day: "numeric",
    month: "short",
    year: "numeric",
  });
}

function formatTime(iso: string): string {
  return new Date(iso).toLocaleTimeString("en-IN", {
    timeZone: getTimezone(),
    hour: "2-digit",
    minute: "2-digit",
  });
}

export function NewsFeedPage() {
  const [symbol, setSymbol] = useState<string>("");
  const [sourceFilter, setSourceFilter] = useState<string>("");
  const [dateFrom, setDateFrom] = useState<string>("");

  // When a date is picked, filter to that single day (date_from + date_to)
  const dateTo = dateFrom
    ? (() => {
        const [y, m, d] = dateFrom.split("-").map(Number);
        const next = new Date(y, m - 1, d + 1);
        const ny = next.getFullYear();
        const nm = String(next.getMonth() + 1).padStart(2, "0");
        const nd = String(next.getDate()).padStart(2, "0");
        return `${ny}-${nm}-${nd}`;
      })()
    : undefined;

  const {
    data,
    isLoading,
    fetchNextPage,
    hasNextPage,
    isFetchingNextPage,
  } = useNewsInfinite({
    symbol: symbol || undefined,
    source: sourceFilter || undefined,
    date_from: dateFrom || undefined,
    date_to: dateTo,
  });

  // Flatten all pages into one list (all filtering is now server-side)
  const filtered = useMemo(
    () => data?.pages.flat() ?? [],
    [data]
  );
  const allArticles = filtered;

  // Group by date
  const grouped = useMemo(() => {
    const groups: { label: string; articles: NewsArticle[] }[] = [];
    const map = new Map<string, NewsArticle[]>();
    for (const a of filtered) {
      const key = a.published_at ? formatDateKey(a.published_at) : "Unknown";
      if (!map.has(key)) map.set(key, []);
      map.get(key)!.push(a);
    }
    for (const [label, arts] of map) {
      groups.push({ label, articles: arts });
    }
    return groups;
  }, [filtered]);

  // Extract unique symbols from filtered articles for the sentiment panel
  const symbolsInFeed = Array.from(
    new Set(filtered.flatMap((a) => a.symbols))
  ).slice(0, 20);

  // All sources present in the unfiltered feed
  const allSources = Array.from(
    new Set(allArticles.map((a) => a.source))
  );

  const toggleSource = (source: string) => {
    setSourceFilter((prev) => (prev === source ? "" : source));
  };

  const activeFilterCount =
    (sourceFilter ? 1 : 0) + (dateFrom ? 1 : 0) + (symbol ? 1 : 0);

  // Infinite scroll: observe a sentinel element at the bottom
  const scrollRef = useRef<HTMLDivElement>(null);
  const sentinelRef = useRef<HTMLDivElement>(null);

  const handleIntersect = useCallback(
    (entries: IntersectionObserverEntry[]) => {
      if (entries[0]?.isIntersecting && hasNextPage && !isFetchingNextPage) {
        fetchNextPage();
      }
    },
    [hasNextPage, isFetchingNextPage, fetchNextPage]
  );

  useEffect(() => {
    const sentinel = sentinelRef.current;
    const root = scrollRef.current;
    if (!sentinel || !root) return;

    const observer = new IntersectionObserver(handleIntersect, {
      root,
      rootMargin: "200px",
    });
    observer.observe(sentinel);
    return () => observer.disconnect();
  }, [handleIntersect]);

  return (
    <div className="flex flex-col h-full -m-3 sm:-m-4 md:-m-6">
      {/* Fixed header with filters */}
      <div className="shrink-0 px-3 sm:px-4 md:px-6 pt-3 sm:pt-4 md:pt-6 pb-3 border-b border-gray-800 bg-gray-950">
        <div className="flex flex-wrap items-center justify-between gap-3 mb-3">
          <h2 className="text-lg font-semibold">News Feed</h2>
          {activeFilterCount > 0 && (
            <button
              onClick={() => {
                setSymbol("");
                setSourceFilter("");
                setDateFrom("");
              }}
              className="text-xs text-gray-500 hover:text-gray-300"
            >
              Clear all filters ({activeFilterCount})
            </button>
          )}
        </div>
        <div className="flex flex-wrap items-center gap-3">
          <input
            type="text"
            placeholder="Filter by symbol..."
            value={symbol}
            onChange={(e) => setSymbol(e.target.value.toUpperCase())}
            className="bg-gray-800 border border-gray-700 rounded px-3 py-1.5 text-sm text-gray-100 w-32 sm:w-36"
          />
          <input
            type="date"
            value={dateFrom}
            onChange={(e) => setDateFrom(e.target.value)}
            className="bg-gray-800 border border-gray-700 rounded px-3 py-1.5 text-sm text-gray-100"
          />
          <select
            value={sourceFilter}
            onChange={(e) => setSourceFilter(e.target.value)}
            className="bg-gray-800 border border-gray-700 rounded px-2 py-1.5 text-sm text-gray-100"
          >
            <option value="">All sources</option>
            {Object.entries(sourceColors).map(([key, { label }]) => (
              <option key={key} value={key}>
                {label}
              </option>
            ))}
            {allSources
              .filter((s) => !sourceColors[s])
              .map((s) => (
                <option key={s} value={s}>
                  {s}
                </option>
              ))}
          </select>
          <span className="text-xs text-gray-500">
            {filtered.length} article{filtered.length !== 1 ? "s" : ""}
          </span>
        </div>
      </div>

      {/* Content area */}
      <div className="flex-1 min-h-0 flex">
        {/* Scrollable news feed */}
        <div ref={scrollRef} className="flex-1 overflow-y-auto px-3 sm:px-4 md:px-6 py-4">
          {isLoading ? (
            <div className="space-y-3">
              {Array.from({ length: 5 }).map((_, i) => (
                <div
                  key={i}
                  className="h-20 animate-pulse bg-gray-900 rounded-lg"
                />
              ))}
            </div>
          ) : filtered.length === 0 ? (
            <div className="bg-gray-900 border border-gray-800 rounded-lg p-6">
              <p className="text-gray-500 text-sm">No news articles found</p>
            </div>
          ) : (
            <div className="space-y-6">
              {grouped.map((group) => (
                <div key={group.label}>
                  {/* Date header */}
                  <div className="flex items-center gap-3 mb-3">
                    <h3 className="text-xs font-semibold text-gray-500 uppercase tracking-wide whitespace-nowrap">
                      {group.label}
                    </h3>
                    <div className="flex-1 border-t border-gray-800" />
                    <span className="text-xs text-gray-600">
                      {group.articles.length}
                    </span>
                  </div>

                  {/* Articles in group */}
                  <div className="space-y-2">
                    {group.articles.map((article) => (
                      <div
                        key={article.content_hash}
                        className="bg-gray-900 border border-gray-800 rounded-lg p-3 hover:border-gray-700 transition-colors"
                      >
                        <p className="text-sm text-gray-200 line-clamp-2">
                          {article.headline}
                        </p>
                        <div className="flex items-center gap-2 mt-2 flex-wrap">
                          <SourceChip
                            source={article.source}
                            active={sourceFilter === article.source}
                            onClick={() => toggleSource(article.source)}
                          />
                          <span className="text-xs text-gray-500">
                            {article.published_at
                              ? formatTime(article.published_at)
                              : ""}
                          </span>
                          {article.url && (
                            <a
                              href={article.url}
                              target="_blank"
                              rel="noopener noreferrer"
                              className="text-xs text-gray-500 hover:text-blue-400 transition-colors"
                              title="Open original article"
                            >
                              &#8599;
                            </a>
                          )}
                          {article.symbols.length > 0 && (
                            <div className="flex gap-1 flex-wrap">
                              {article.symbols.map((s) => (
                                <span
                                  key={s}
                                  className="px-1.5 py-0.5 rounded bg-gray-800 text-emerald-400 text-xs cursor-pointer hover:bg-gray-700"
                                  onClick={() => setSymbol(s)}
                                >
                                  {s}
                                </span>
                              ))}
                            </div>
                          )}
                        </div>
                      </div>
                    ))}
                  </div>
                </div>
              ))}

              {/* Sentinel + loading indicator */}
              <div ref={sentinelRef} className="py-2">
                {isFetchingNextPage && (
                  <div className="flex items-center justify-center gap-2 py-4 text-gray-500 text-sm">
                    <div className="w-4 h-4 border-2 border-gray-600 border-t-emerald-400 rounded-full animate-spin" />
                    Loading more...
                  </div>
                )}
                {!hasNextPage && allArticles.length > 0 && (
                  <p className="text-center text-xs text-gray-600 py-2">
                    All articles loaded
                  </p>
                )}
              </div>
            </div>
          )}
        </div>

        {/* Fixed right sidebar */}
        <div className="hidden lg:block w-64 shrink-0 border-l border-gray-800 overflow-y-auto p-4 space-y-4">
          {/* Sources */}
          <div>
            <div className="flex items-center justify-between mb-3">
              <h3 className="text-sm font-medium text-gray-400">Sources</h3>
              {sourceFilter && (
                <button
                  onClick={() => setSourceFilter("")}
                  className="text-xs text-gray-500 hover:text-gray-300"
                >
                  Clear
                </button>
              )}
            </div>
            <div className="space-y-2">
              {Object.keys(sourceColors).map((source) => {
                const count = allArticles.filter(
                  (a) => a.source === source
                ).length;
                return (
                  <div
                    key={source}
                    className="flex items-center justify-between"
                  >
                    <SourceChip
                      source={source}
                      active={sourceFilter === source}
                      onClick={() => toggleSource(source)}
                    />
                    <span className="text-xs text-gray-500">{count}</span>
                  </div>
                );
              })}
              {allSources
                .filter((s) => !sourceColors[s])
                .map((source) => {
                  const count = allArticles.filter(
                    (a) => a.source === source
                  ).length;
                  return (
                    <div
                      key={source}
                      className="flex items-center justify-between"
                    >
                      <SourceChip
                        source={source}
                        active={sourceFilter === source}
                        onClick={() => toggleSource(source)}
                      />
                      <span className="text-xs text-gray-500">{count}</span>
                    </div>
                  );
                })}
            </div>
          </div>

          {/* Sentiment overview */}
          <div>
            <h3 className="text-sm font-medium text-gray-400 mb-3">
              Sentiment Overview
            </h3>
            {symbolsInFeed.length === 0 ? (
              <p className="text-gray-500 text-xs">
                No symbols in current feed
              </p>
            ) : (
              <div className="space-y-2">
                {symbolsInFeed.map((s) => (
                  <div
                    key={s}
                    className="flex items-center justify-between py-1 border-b border-gray-800/50 last:border-0"
                  >
                    <span
                      className="text-sm font-medium text-emerald-400 cursor-pointer hover:underline"
                      onClick={() => setSymbol(s)}
                    >
                      {s}
                    </span>
                    <SentimentBadge symbol={s} />
                  </div>
                ))}
              </div>
            )}
          </div>
        </div>
      </div>
    </div>
  );
}
