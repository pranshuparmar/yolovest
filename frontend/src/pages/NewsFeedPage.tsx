import { useState } from "react";
import { useNews, useSentiment } from "../hooks/queries";
import clsx from "clsx";

const sourceColors: Record<string, string> = {
  MoneyControl: "bg-blue-900/40 text-blue-400",
  "ET Markets": "bg-purple-900/40 text-purple-400",
  LiveMint: "bg-emerald-900/40 text-emerald-400",
  "NSE Official": "bg-amber-900/40 text-amber-400",
  "Google Finance": "bg-red-900/40 text-red-400",
};

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
        sourceColors[source] || "bg-gray-800 text-gray-400",
        active && "ring-1 ring-current",
        "cursor-pointer hover:opacity-80"
      )}
    >
      {source}
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

export function NewsFeedPage() {
  const [symbol, setSymbol] = useState<string>("");
  const [sourceFilter, setSourceFilter] = useState<string>("");
  const [limit, setLimit] = useState(50);
  const { data: articles, isLoading } = useNews({
    symbol: symbol || undefined,
    limit,
  });

  // Apply client-side source filter
  const filtered = sourceFilter
    ? (articles || []).filter((a) => a.source === sourceFilter)
    : articles || [];

  // Extract unique symbols from filtered articles for the sentiment panel
  const symbolsInFeed = Array.from(
    new Set(filtered.flatMap((a) => a.symbols))
  ).slice(0, 20);

  // All sources present in the unfiltered feed
  const allSources = Array.from(
    new Set((articles || []).map((a) => a.source))
  );

  const toggleSource = (source: string) => {
    setSourceFilter((prev) => (prev === source ? "" : source));
  };

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <h2 className="text-lg font-semibold">News Feed</h2>
        <div className="flex items-center gap-3">
          <input
            type="text"
            placeholder="Filter by symbol..."
            value={symbol}
            onChange={(e) => setSymbol(e.target.value.toUpperCase())}
            className="bg-gray-800 border border-gray-700 rounded px-3 py-1 text-sm text-gray-100 w-40"
          />
          <select
            value={limit}
            onChange={(e) => setLimit(Number(e.target.value))}
            className="bg-gray-800 border border-gray-700 rounded px-2 py-1 text-sm text-gray-100"
          >
            <option value={25}>25 articles</option>
            <option value={50}>50 articles</option>
            <option value={100}>100 articles</option>
            <option value={200}>200 articles</option>
          </select>
        </div>
      </div>

      <div className="grid grid-cols-1 lg:grid-cols-4 gap-6">
        {/* News articles */}
        <div className="lg:col-span-3 space-y-3">
          {isLoading ? (
            <div className="space-y-3">
              {Array.from({ length: 5 }).map((_, i) => (
                <div
                  key={i}
                  className="h-24 animate-pulse bg-gray-900 rounded-lg"
                />
              ))}
            </div>
          ) : filtered.length === 0 ? (
            <div className="bg-gray-900 border border-gray-800 rounded-lg p-6">
              <p className="text-gray-500 text-sm">No news articles found</p>
              {sourceFilter && (
                <button
                  onClick={() => setSourceFilter("")}
                  className="text-xs text-emerald-400 hover:underline mt-1"
                >
                  Clear source filter
                </button>
              )}
            </div>
          ) : (
            filtered.map((article) => (
              <div
                key={article.content_hash}
                className="bg-gray-900 border border-gray-800 rounded-lg p-4 hover:border-gray-700 transition-colors"
              >
                <div className="flex-1 min-w-0">
                  <a
                    href={article.url}
                    target="_blank"
                    rel="noopener noreferrer"
                    className="text-sm text-gray-200 hover:text-emerald-400 transition-colors line-clamp-2"
                  >
                    {article.headline}
                  </a>
                  <div className="flex items-center gap-2 mt-2 flex-wrap">
                    <SourceChip
                      source={article.source}
                      active={sourceFilter === article.source}
                      onClick={() => toggleSource(article.source)}
                    />
                    <a
                      href={article.url}
                      target="_blank"
                      rel="noopener noreferrer"
                      className="text-xs text-gray-500 hover:text-emerald-400 transition-colors"
                      title="Open original article"
                    >
                      {article.published_at
                        ? new Date(article.published_at).toLocaleString(
                            "en-IN",
                            {
                              month: "short",
                              day: "numeric",
                              hour: "2-digit",
                              minute: "2-digit",
                            }
                          )
                        : "View source"}
                      {" \u2197"}
                    </a>
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
              </div>
            ))
          )}
        </div>

        {/* Right sidebar */}
        <div className="space-y-4">
          {/* Sources — clickable to filter */}
          <div className="bg-gray-900 border border-gray-800 rounded-lg p-4">
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
                const count = (articles || []).filter(
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
              {/* Show any extra sources not in the predefined list */}
              {allSources
                .filter((s) => !sourceColors[s])
                .map((source) => {
                  const count = (articles || []).filter(
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
          <div className="bg-gray-900 border border-gray-800 rounded-lg p-4">
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
                    className="flex items-center justify-between py-1.5 border-b border-gray-800/50 last:border-0"
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
