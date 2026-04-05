import { useState, useEffect, useCallback } from "react";
import { useConfig, useUpdateConfig } from "../hooks/queries";

/** Human-readable labels for config sections */
const SECTION_LABELS: Record<string, string> = {
  capital: "Capital",
  llm: "LLM (Gemini)",
  market_data: "Market Data",
  heartbeat: "Heartbeat",
  scanning: "Scanning",
  strategy: "Strategy",
  risk: "Risk Management",
  market_hours: "Market Hours",
  execution: "Execution",
  transaction_costs: "Transaction Costs",
  database: "Database",
  retraining: "Retraining",
  reports: "Reports",
  dashboard: "Dashboard",
  notifications: "Notifications",
  news_digest: "News Digest",
};

/** Human-readable labels for individual keys (last segment) */
const KEY_LABELS: Record<string, string> = {
  initial_amount: "Initial Capital (INR)",
  enabled: "Enabled",
  model: "Model",
  daily_provider: "Daily Provider",
  daily_fallback: "Daily Fallback",
  intraday_provider: "Intraday Provider",
  kite_data_enabled: "Kite Data Enabled",
  news_enabled: "News Enabled",
  scrapers_enabled: "Scrapers Enabled",
  cache_ttl_minutes: "Cache TTL (min)",
  stale_threshold_minutes: "Stale Threshold (min)",
  sentiment_ttl_hours: "Sentiment TTL (hrs)",
  backfill_days: "Backfill Days",
  market_hours_interval_min: "Market Hours Interval (min)",
  off_hours_interval_min: "Off Hours Interval (min)",
  max_consecutive_skips: "Max Consecutive Skips",
  universe: "Universe",
  shortlist_size: "Shortlist Size",
  min_avg_daily_volume: "Min Avg Daily Volume",
  mode: "Strategy Mode",
  max_risk_per_trade_pct: "Max Risk/Trade (%)",
  max_portfolio_exposure_pct: "Max Portfolio Exposure (%)",
  max_open_positions: "Max Open Positions",
  max_single_stock_pct: "Max Single Stock (%)",
  daily_loss_limit_pct: "Daily Loss Limit (%)",
  weekly_loss_limit_pct: "Weekly Loss Limit (%)",
  mandatory_stop_loss: "Mandatory Stop Loss",
  trailing_sl_enabled: "Trailing SL Enabled",
  min_confidence_score: "Min Confidence Score",
  max_trades_per_day: "Max Trades/Day",
  kill_switch_enabled: "Kill Switch Enabled",
  llm_review_enabled: "LLM Review Enabled",
  max_same_sector_positions: "Max Same Sector Positions",
  price_drift_max_pct: "Price Drift Max (%)",
  transaction_mode: "Transaction Mode",
  paper_slippage_pct: "Paper Slippage (%)",
  show_degraded_banner: "Show Degraded Banner",
  schedule_cron: "Schedule (cron)",
  daily_report_time: "Daily Report Time",
  max_headlines: "Max Headlines",
};

function getKeyLabel(fullKey: string): string {
  const parts = fullKey.split(".");
  const last = parts[parts.length - 1];
  return KEY_LABELS[last] ?? last.replace(/_/g, " ").replace(/\b\w/g, (c) => c.toUpperCase());
}

function InputField({
  fullKey,
  value,
  onChange,
}: {
  fullKey: string;
  value: unknown;
  onChange: (key: string, val: unknown) => void;
}) {
  const label = getKeyLabel(fullKey);

  if (typeof value === "boolean") {
    return (
      <label className="flex items-center justify-between py-2 px-3 rounded hover:bg-gray-800/50 cursor-pointer">
        <span className="text-sm text-gray-300">{label}</span>
        <input
          type="checkbox"
          checked={value}
          onChange={(e) => onChange(fullKey, e.target.checked)}
          className="w-4 h-4 accent-blue-500"
        />
      </label>
    );
  }

  if (typeof value === "number") {
    return (
      <label className="flex items-center justify-between gap-4 py-2 px-3 rounded hover:bg-gray-800/50">
        <span className="text-sm text-gray-300 shrink-0">{label}</span>
        <input
          type="number"
          step={value % 1 !== 0 ? 0.001 : 1}
          value={value}
          onChange={(e) => {
            const v = e.target.value;
            onChange(fullKey, v.includes(".") ? parseFloat(v) : parseInt(v, 10));
          }}
          className="w-32 bg-gray-800 border border-gray-700 rounded px-2 py-1 text-sm text-gray-200 text-right focus:border-blue-500 focus:outline-none"
        />
      </label>
    );
  }

  if (typeof value === "string") {
    return (
      <label className="flex items-center justify-between gap-4 py-2 px-3 rounded hover:bg-gray-800/50">
        <span className="text-sm text-gray-300 shrink-0">{label}</span>
        <input
          type="text"
          value={value}
          onChange={(e) => onChange(fullKey, e.target.value)}
          className="w-48 bg-gray-800 border border-gray-700 rounded px-2 py-1 text-sm text-gray-200 text-right focus:border-blue-500 focus:outline-none"
        />
      </label>
    );
  }

  // Arrays and objects: show as JSON textarea
  return (
    <label className="flex flex-col gap-1 py-2 px-3 rounded hover:bg-gray-800/50">
      <span className="text-sm text-gray-300">{label}</span>
      <textarea
        value={JSON.stringify(value, null, 2)}
        onChange={(e) => {
          try {
            onChange(fullKey, JSON.parse(e.target.value));
          } catch {
            // ignore invalid JSON while typing
          }
        }}
        rows={3}
        className="bg-gray-800 border border-gray-700 rounded px-2 py-1 text-sm text-gray-200 font-mono focus:border-blue-500 focus:outline-none"
      />
    </label>
  );
}

export default function SettingsPage() {
  const { data, isLoading, error } = useConfig();
  const updateMutation = useUpdateConfig();

  // Local edited state: tracks pending changes
  const [edited, setEdited] = useState<Record<string, unknown>>({});
  // Full local copy of config values (for rendering)
  const [localConfig, setLocalConfig] = useState<Record<string, Record<string, unknown>>>({});
  const [expandedSections, setExpandedSections] = useState<Record<string, boolean>>({});
  const [saveMsg, setSaveMsg] = useState<string | null>(null);

  useEffect(() => {
    if (data?.sections) {
      setLocalConfig(data.sections as Record<string, Record<string, unknown>>);
      setEdited({});
    }
  }, [data]);

  const handleChange = useCallback((key: string, value: unknown) => {
    setEdited((prev) => ({ ...prev, [key]: value }));
    // Update local copy for immediate UI feedback
    setLocalConfig((prev) => {
      const section = key.split(".")[0];
      return {
        ...prev,
        [section]: { ...prev[section], [key]: value },
      };
    });
  }, []);

  const handleSave = useCallback(() => {
    if (Object.keys(edited).length === 0) return;
    setSaveMsg(null);
    updateMutation.mutate(edited, {
      onSuccess: (result) => {
        setEdited({});
        setSaveMsg(`Saved ${result.updated.length} setting(s)`);
        setTimeout(() => setSaveMsg(null), 3000);
      },
      onError: (err) => {
        setSaveMsg(`Error: ${err instanceof Error ? err.message : String(err)}`);
      },
    });
  }, [edited, updateMutation]);

  const handleReset = useCallback(() => {
    if (data?.sections) {
      setLocalConfig(data.sections as Record<string, Record<string, unknown>>);
      setEdited({});
    }
  }, [data]);

  const toggleSection = (section: string) => {
    setExpandedSections((prev) => ({ ...prev, [section]: !prev[section] }));
  };

  if (isLoading) {
    return (
      <div className="p-6 text-gray-400">Loading configuration...</div>
    );
  }

  if (error) {
    return (
      <div className="p-6 text-red-400">
        Failed to load configuration: {error instanceof Error ? error.message : "Unknown error"}
      </div>
    );
  }

  const sections = Object.keys(localConfig).sort(
    (a, b) => (Object.keys(SECTION_LABELS).indexOf(a) + 1 || 99) - (Object.keys(SECTION_LABELS).indexOf(b) + 1 || 99)
  );

  const pendingCount = Object.keys(edited).length;

  return (
    <div className="p-4 md:p-6 max-w-4xl mx-auto space-y-4">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-xl font-bold text-gray-100">Settings</h1>
          <p className="text-sm text-gray-500 mt-0.5">
            Configure trading parameters. Changes take effect immediately.
          </p>
        </div>
        <div className="flex items-center gap-2">
          {pendingCount > 0 && (
            <>
              <span className="text-xs text-amber-400">
                {pendingCount} unsaved change{pendingCount > 1 ? "s" : ""}
              </span>
              <button
                onClick={handleReset}
                className="px-3 py-1.5 rounded text-sm bg-gray-700 hover:bg-gray-600 text-gray-300"
              >
                Discard
              </button>
            </>
          )}
          <button
            onClick={handleSave}
            disabled={pendingCount === 0 || updateMutation.isPending}
            className="px-4 py-1.5 rounded text-sm font-medium bg-blue-600 hover:bg-blue-700 text-white disabled:opacity-40 disabled:cursor-not-allowed"
          >
            {updateMutation.isPending ? "Saving..." : "Save"}
          </button>
        </div>
      </div>

      {saveMsg && (
        <div
          className={`text-sm px-3 py-2 rounded ${
            saveMsg.startsWith("Error") ? "bg-red-900/30 text-red-400" : "bg-emerald-900/30 text-emerald-400"
          }`}
        >
          {saveMsg}
        </div>
      )}

      <div className="space-y-2">
        {sections.map((section) => {
          const entries = Object.entries(localConfig[section] ?? {});
          const isExpanded = expandedSections[section] ?? false;
          const sectionLabel = SECTION_LABELS[section] ?? section;
          const changedKeys = entries.filter(([k]) => k in edited).length;

          return (
            <div key={section} className="bg-gray-900 border border-gray-800 rounded-lg overflow-hidden">
              <button
                onClick={() => toggleSection(section)}
                className="w-full flex items-center justify-between px-4 py-3 hover:bg-gray-800/50 transition-colors"
              >
                <div className="flex items-center gap-2">
                  <svg
                    className={`w-4 h-4 text-gray-500 transition-transform ${isExpanded ? "rotate-90" : ""}`}
                    fill="none" viewBox="0 0 24 24" stroke="currentColor"
                  >
                    <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M9 5l7 7-7 7" />
                  </svg>
                  <span className="text-sm font-medium text-gray-200">{sectionLabel}</span>
                  <span className="text-xs text-gray-600">{entries.length} settings</span>
                </div>
                {changedKeys > 0 && (
                  <span className="text-xs bg-amber-900/40 text-amber-400 px-2 py-0.5 rounded">
                    {changedKeys} changed
                  </span>
                )}
              </button>
              {isExpanded && (
                <div className="border-t border-gray-800 px-2 py-1 divide-y divide-gray-800/50">
                  {entries.map(([key, value]) => (
                    <InputField key={key} fullKey={key} value={value} onChange={handleChange} />
                  ))}
                </div>
              )}
            </div>
          );
        })}
      </div>
    </div>
  );
}
