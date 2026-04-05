import { useState, useEffect, useCallback } from "react";
import { useConfig, useUpdateConfig } from "../hooks/queries";
import clsx from "clsx";

// ---------------------------------------------------------------------------
// Tab definitions
// ---------------------------------------------------------------------------

interface Tab {
  id: string;
  label: string;
  sections: string[]; // ordered list of section keys to show
}

const TABS: Tab[] = [
  {
    id: "general",
    label: "General",
    sections: ["_general_top", "llm", "market_data", "heartbeat", "news_digest", "dashboard"],
  },
  {
    id: "strategy",
    label: "Strategy",
    sections: ["scanning", "strategy"],
  },
  {
    id: "risk",
    label: "Risk & Execution",
    sections: ["risk", "execution", "transaction_costs"],
  },
  {
    id: "schedule",
    label: "Schedules",
    sections: ["_cron_schedules", "market_hours", "database"],
  },
  {
    id: "notifications",
    label: "Notifications",
    sections: ["notifications"],
  },
];

// ---------------------------------------------------------------------------
// Virtual sections — pull keys from multiple real sections
// ---------------------------------------------------------------------------

// Keys shown in the "General > Top" card (mode + capital)
const GENERAL_TOP_KEYS = ["mode", "capital.initial_amount", "log.level", "log.file_level"];

// All cron-related keys, pulled from various sections into one card
const CRON_KEYS = [
  "scanning.universe_cron",
  "news_digest.schedule_cron",
  "reports.daily_report_time",
  "reports.weekly_report_cron",
  "retraining.schedule_cron",
  "database.backup_cron",
];

// Keys to hide from their original sections (shown in virtual sections instead)
const RELOCATED_KEYS = new Set([...GENERAL_TOP_KEYS, ...CRON_KEYS]);

// ---------------------------------------------------------------------------
// Enum options for select fields
// ---------------------------------------------------------------------------

const SELECT_OPTIONS: Record<string, { value: string; label: string }[]> = {
  "mode": [
    { value: "paper", label: "Paper Trading" },
    { value: "live", label: "Live Trading" },
  ],
  "strategy.mode": [
    { value: "intraday", label: "Intraday" },
    { value: "short_term", label: "Short Term" },
    { value: "balanced", label: "Balanced" },
    { value: "long_term", label: "Long Term" },
  ],
  "strategy.default_trade_type": [
    { value: "intraday", label: "Intraday" },
    { value: "swing", label: "Swing" },
  ],
  "scanning.universe": [
    { value: "nifty50", label: "Nifty 50" },
    { value: "nifty500", label: "Nifty 500" },
    { value: "all", label: "All" },
  ],
  "execution.transaction_mode": [
    { value: "auto", label: "Auto (execute immediately)" },
    { value: "manual", label: "Manual (require approval)" },
  ],
  "risk.holding_expiry.action": [
    { value: "tighten_or_close", label: "Tighten or Close" },
    { value: "force_close", label: "Force Close" },
    { value: "ignore", label: "Ignore" },
  ],
  "risk.weekly_reset_day": [
    { value: "monday", label: "Monday" },
    { value: "tuesday", label: "Tuesday" },
    { value: "wednesday", label: "Wednesday" },
    { value: "thursday", label: "Thursday" },
    { value: "friday", label: "Friday" },
  ],
  "log.level": [
    { value: "DEBUG", label: "Debug" },
    { value: "INFO", label: "Info" },
    { value: "WARNING", label: "Warning" },
    { value: "ERROR", label: "Error" },
  ],
  "log.file_level": [
    { value: "DEBUG", label: "Debug" },
    { value: "INFO", label: "Info" },
    { value: "WARNING", label: "Warning" },
    { value: "ERROR", label: "Error" },
  ],
};

// Read-only informational fields (current provider implementations)
const READ_ONLY_KEYS = new Set([
  "market_data.daily_provider",
  "market_data.daily_fallback",
  "market_data.intraday_provider",
  "market_hours.timezone",
]);

// ---------------------------------------------------------------------------
// Section labels
// ---------------------------------------------------------------------------

const SECTION_LABELS: Record<string, string> = {
  _general_top: "General",
  _cron_schedules: "Cron Schedules",
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
  database: "Data Retention & Backups",
  retraining: "Model Retraining",
  reports: "Reports",
  dashboard: "Dashboard",
  notifications: "Notifications",
  news_digest: "News Digest",
};

const KEY_LABELS: Record<string, string> = {
  mode: "Trading Mode",
  initial_amount: "Initial Capital (INR)",
  enabled: "Enabled",
  model: "Gemini Model",
  daily_provider: "Daily Provider",
  daily_fallback: "Daily Fallback",
  intraday_provider: "Intraday Provider",
  kite_data_enabled: "Kite Data (paid)",
  news_enabled: "News Sources",
  scrapers_enabled: "Scrapers",
  cache_ttl_minutes: "Cache TTL (min)",
  stale_threshold_minutes: "Stale Threshold (min)",
  sentiment_ttl_hours: "Sentiment TTL (hrs)",
  backfill_days: "Backfill History (days)",
  market_hours_interval_min: "Market Hours Interval (min)",
  off_hours_interval_min: "Off Hours Interval (min)",
  max_consecutive_skips: "Max Consecutive Skips",
  universe: "Stock Universe",
  universe_cron: "Universe Update",
  shortlist_size: "Shortlist Size",
  min_avg_daily_volume: "Min Avg Daily Volume",
  max_risk_per_trade_pct: "Max Risk / Trade",
  max_portfolio_exposure_pct: "Max Portfolio Exposure",
  max_open_positions: "Max Open Positions",
  max_single_stock_pct: "Max Single Stock Exposure",
  daily_loss_limit_pct: "Daily Loss Limit",
  weekly_loss_limit_pct: "Weekly Loss Limit",
  weekly_loss_sizing_reduction: "Weekly Loss Size Reduction",
  mandatory_stop_loss: "Mandatory Stop Loss",
  trailing_sl_enabled: "Trailing Stop Loss",
  trailing_sl_trigger_multiple: "Trailing SL Trigger (x risk)",
  trailing_sl_step_pct: "Trailing SL Step",
  min_confidence_score: "Min Confidence Score",
  max_trades_per_day: "Max Trades / Day",
  kill_switch_enabled: "Kill Switch",
  kill_switch_persistent: "Kill Switch Survives Restart",
  llm_review_enabled: "LLM Trade Review",
  llm_fallback_to_rules: "Fallback to Rules if LLM Down",
  max_same_sector_positions: "Max Same Sector Positions",
  margin_usage_enabled: "Margin / Leverage",
  weekly_reset_day: "Weekly PnL Reset Day",
  loss_cooldown_minutes: "Loss Cooldown (min)",
  symbol_cooldown_days: "Symbol Cooldown (days)",
  symbol_repeat_lookback_days: "Repeat Symbol Lookback (days)",
  symbol_repeat_min_confidence: "Repeat Symbol Min Confidence",
  price_drift_max_pct: "Max Price Drift",
  transaction_mode: "Transaction Mode",
  paper_slippage_pct: "Paper Slippage",
  max_order_retries: "Max Order Retries",
  retry_base_delay_sec: "Retry Base Delay (sec)",
  order_timeout_sec: "Order Timeout (sec)",
  show_degraded_banner: "Show Degraded Banner",
  schedule_cron: "Schedule (cron)",
  daily_report_time: "Daily Report Time",
  weekly_report_cron: "Weekly Report",
  max_headlines: "Max Headlines",
  backup_enabled: "Backups Enabled",
  backup_cron: "Backup Schedule",
  ohlcv_days: "OHLCV Retention (days)",
  audit_log_days: "Audit Log Retention (days)",
  predictions_days: "Predictions Retention (days)",
  news_days: "News Retention (days)",
  economic_events_days: "Economic Events Retention (days)",
  shadow_mode_days: "Shadow Mode Duration (days)",
  shadow_min_predictions: "Shadow Min Predictions",
  retired_model_cleanup_days: "Retired Model Cleanup (days)",
  brokerage_per_leg_pct: "Brokerage / Leg",
  brokerage_cap_per_leg: "Brokerage Cap / Leg (INR)",
  stt_intraday_pct: "STT Intraday",
  stt_delivery_pct: "STT Delivery",
  other_charges_pct: "Other Charges",
  open: "Market Open",
  close: "Market Close",
  order_start: "Order Start",
  order_end: "Order End",
  square_off: "Square Off",
  square_off_extension: "Square Off Extension",
  timezone: "Timezone",
  default_trade_type: "Default Trade Type",
  min_training_samples: "Min Training Samples",
  level: "Console Log Level",
  file_level: "File Log Level",
  trade_entry: "Trade Entry Alerts",
  trade_exit: "Trade Exit Alerts",
  daily_summary: "Daily Summary",
  weekly_summary: "Weekly Summary",
  errors: "Error Alerts",
  action: "Expiry Action",
  breakeven_buffer_pct: "Breakeven Buffer",
  loss_threshold_pct: "Loss Threshold",
  max_holding_days: "Max Holding Days",
};

// Cron key labels (friendly names for the virtual cron section)
const CRON_LABELS: Record<string, string> = {
  "scanning.universe_cron": "Universe Refresh",
  "news_digest.schedule_cron": "News Digest",
  "reports.daily_report_time": "Daily Report (time)",
  "reports.weekly_report_cron": "Weekly Report",
  "retraining.schedule_cron": "Model Retraining",
  "database.backup_cron": "Database Backup",
};

function getKeyLabel(fullKey: string): string {
  // Check cron labels first (full key match)
  if (CRON_LABELS[fullKey]) return CRON_LABELS[fullKey];
  const last = fullKey.split(".").pop()!;
  return KEY_LABELS[last] ?? last.replace(/_/g, " ").replace(/\b\w/g, (c) => c.toUpperCase());
}

function formatHint(fullKey: string): string | null {
  if (fullKey.includes("_pct")) return "0–1 (e.g. 0.02 = 2%)";
  if (fullKey.includes("_cron") || CRON_KEYS.includes(fullKey)) return "cron expression";
  return null;
}

// ---------------------------------------------------------------------------
// Field components
// ---------------------------------------------------------------------------

function ToggleField({
  label,
  checked,
  onChange,
  disabled,
}: {
  label: string;
  checked: boolean;
  onChange: (val: boolean) => void;
  disabled?: boolean;
}) {
  return (
    <label className={clsx("flex items-center justify-between py-2.5 group", !disabled && "cursor-pointer")}>
      <span className={clsx("text-sm", disabled ? "text-gray-500" : "text-gray-300 group-hover:text-gray-100")}>{label}</span>
      <button
        type="button"
        role="switch"
        aria-checked={checked}
        disabled={disabled}
        onClick={() => !disabled && onChange(!checked)}
        className={clsx(
          "relative w-9 h-5 rounded-full transition-colors",
          checked ? "bg-blue-600" : "bg-gray-700",
          disabled && "opacity-50 cursor-not-allowed",
        )}
      >
        <span className={clsx("absolute top-0.5 left-0.5 w-4 h-4 rounded-full bg-white transition-transform", checked && "translate-x-4")} />
      </button>
    </label>
  );
}

function SelectField({
  label,
  value,
  options,
  onChange,
}: {
  label: string;
  value: string;
  options: { value: string; label: string }[];
  onChange: (val: string) => void;
}) {
  return (
    <div className="flex items-center justify-between py-2.5 gap-4">
      <span className="text-sm text-gray-300">{label}</span>
      <select
        value={value}
        onChange={(e) => onChange(e.target.value)}
        className="bg-gray-800 border border-gray-700 rounded px-2.5 py-1.5 text-sm text-gray-200 focus:border-blue-500 focus:outline-none"
      >
        {options.map((o) => (
          <option key={o.value} value={o.value}>{o.label}</option>
        ))}
      </select>
    </div>
  );
}

function ReadOnlyField({
  label,
  value,
}: {
  label: string;
  value: string;
}) {
  return (
    <div className="flex items-center justify-between py-2.5 gap-4">
      <span className="text-sm text-gray-500">{label}</span>
      <span className="text-sm text-gray-500 bg-gray-800/50 border border-gray-800 rounded px-2.5 py-1.5">{value}</span>
    </div>
  );
}

function NumberField({
  label,
  value,
  hint,
  onChange,
}: {
  label: string;
  value: number;
  hint?: string | null;
  onChange: (val: number) => void;
}) {
  return (
    <div className="flex items-center justify-between py-2.5 gap-4">
      <div className="flex flex-col">
        <span className="text-sm text-gray-300">{label}</span>
        {hint && <span className="text-[10px] text-gray-600">{hint}</span>}
      </div>
      <input
        type="number"
        step={value % 1 !== 0 ? 0.001 : 1}
        value={value}
        onChange={(e) => {
          const v = e.target.value;
          onChange(v.includes(".") ? parseFloat(v) : parseInt(v, 10));
        }}
        className="w-28 bg-gray-800 border border-gray-700 rounded px-2.5 py-1.5 text-sm text-gray-200 text-right focus:border-blue-500 focus:outline-none"
      />
    </div>
  );
}

function TextField({
  label,
  value,
  hint,
  onChange,
}: {
  label: string;
  value: string;
  hint?: string | null;
  onChange: (val: string) => void;
}) {
  return (
    <div className="flex items-center justify-between py-2.5 gap-4">
      <div className="flex flex-col">
        <span className="text-sm text-gray-300">{label}</span>
        {hint && <span className="text-[10px] text-gray-600">{hint}</span>}
      </div>
      <input
        type="text"
        value={value}
        onChange={(e) => onChange(e.target.value)}
        className="w-44 bg-gray-800 border border-gray-700 rounded px-2.5 py-1.5 text-sm text-gray-200 text-right focus:border-blue-500 focus:outline-none"
      />
    </div>
  );
}

function JsonField({
  label,
  value,
  onChange,
}: {
  label: string;
  value: unknown;
  onChange: (val: unknown) => void;
}) {
  return (
    <div className="py-2.5 space-y-1">
      <span className="text-sm text-gray-300">{label}</span>
      <textarea
        value={JSON.stringify(value, null, 2)}
        onChange={(e) => { try { onChange(JSON.parse(e.target.value)); } catch { /* typing */ } }}
        rows={3}
        className="w-full bg-gray-800 border border-gray-700 rounded px-2.5 py-1.5 text-sm text-gray-200 font-mono focus:border-blue-500 focus:outline-none"
      />
    </div>
  );
}

function ConfigField({
  fullKey,
  value,
  onChange,
}: {
  fullKey: string;
  value: unknown;
  onChange: (key: string, val: unknown) => void;
}) {
  const label = getKeyLabel(fullKey);
  const hint = formatHint(fullKey);

  // Read-only informational fields
  if (READ_ONLY_KEYS.has(fullKey)) {
    return <ReadOnlyField label={label} value={String(value)} />;
  }

  // Select fields with known options
  if (SELECT_OPTIONS[fullKey] && typeof value === "string") {
    return <SelectField label={label} value={value} options={SELECT_OPTIONS[fullKey]} onChange={(v) => onChange(fullKey, v)} />;
  }

  if (typeof value === "boolean") {
    return <ToggleField label={label} checked={value} onChange={(v) => onChange(fullKey, v)} />;
  }
  if (typeof value === "number") {
    return <NumberField label={label} value={value} hint={hint} onChange={(v) => onChange(fullKey, v)} />;
  }
  if (typeof value === "string") {
    return <TextField label={label} value={value} hint={hint} onChange={(v) => onChange(fullKey, v)} />;
  }
  return <JsonField label={label} value={value} onChange={(v) => onChange(fullKey, v)} />;
}

// ---------------------------------------------------------------------------
// Section card
// ---------------------------------------------------------------------------

function SectionCard({
  title,
  entries,
  edited,
  onChange,
}: {
  title: string;
  entries: [string, unknown][];
  edited: Record<string, unknown>;
  onChange: (key: string, val: unknown) => void;
}) {
  const changedCount = entries.filter(([k]) => k in edited).length;
  if (entries.length === 0) return null;

  return (
    <div className="bg-gray-900 border border-gray-800 rounded-lg">
      <div className="flex items-center justify-between px-5 pt-4 pb-2">
        <h3 className="text-sm font-semibold text-gray-200">{title}</h3>
        {changedCount > 0 && (
          <span className="text-[10px] bg-amber-900/40 text-amber-400 px-1.5 py-0.5 rounded">
            {changedCount} changed
          </span>
        )}
      </div>
      <div className="px-5 pb-4 divide-y divide-gray-800/60">
        {entries.map(([key, value]) => (
          <ConfigField key={key} fullKey={key} value={value} onChange={onChange} />
        ))}
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Main page
// ---------------------------------------------------------------------------

export default function SettingsPage() {
  const { data, isLoading, error } = useConfig();
  const updateMutation = useUpdateConfig();

  const [activeTab, setActiveTab] = useState("general");
  const [edited, setEdited] = useState<Record<string, unknown>>({});
  const [localConfig, setLocalConfig] = useState<Record<string, Record<string, unknown>>>({});
  const [saveMsg, setSaveMsg] = useState<string | null>(null);

  // Flatten all config into a single lookup for virtual sections
  const flatConfig: Record<string, unknown> = {};
  for (const section of Object.values(localConfig)) {
    for (const [k, v] of Object.entries(section)) {
      flatConfig[k] = v;
    }
  }

  useEffect(() => {
    if (data?.sections) {
      setLocalConfig(data.sections as Record<string, Record<string, unknown>>);
      setEdited({});
    }
  }, [data]);

  const handleChange = useCallback((key: string, value: unknown) => {
    setEdited((prev) => ({ ...prev, [key]: value }));
    setLocalConfig((prev) => {
      const section = key.split(".")[0];
      return { ...prev, [section]: { ...prev[section], [key]: value } };
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

  const handleDiscard = useCallback(() => {
    if (data?.sections) {
      setLocalConfig(data.sections as Record<string, Record<string, unknown>>);
      setEdited({});
    }
  }, [data]);

  // Build entries for a section key — handles virtual sections
  const getEntries = useCallback((sectionKey: string): [string, unknown][] => {
    if (sectionKey === "_general_top") {
      return GENERAL_TOP_KEYS.map((k) => [k, flatConfig[k]] as [string, unknown]).filter(([, v]) => v !== undefined);
    }
    if (sectionKey === "_cron_schedules") {
      return CRON_KEYS.map((k) => [k, flatConfig[k]] as [string, unknown]).filter(([, v]) => v !== undefined);
    }
    // Normal section — filter out relocated keys
    return Object.entries(localConfig[sectionKey] ?? {}).filter(([k]) => !RELOCATED_KEYS.has(k));
  }, [localConfig, flatConfig]);

  if (isLoading) {
    return <div className="p-6 text-gray-400">Loading configuration...</div>;
  }
  if (error) {
    return (
      <div className="p-6 text-red-400">
        Failed to load configuration: {error instanceof Error ? error.message : "Unknown error"}
      </div>
    );
  }

  const pendingCount = Object.keys(edited).length;
  const currentTab = TABS.find((t) => t.id === activeTab) || TABS[0];

  return (
    <div className="space-y-4">
      {/* Header */}
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-xl font-bold text-gray-100">Settings</h1>
          <p className="text-xs text-gray-500 mt-0.5">Changes take effect immediately after saving.</p>
        </div>
        <div className="flex items-center gap-2">
          {pendingCount > 0 && (
            <>
              <span className="text-xs text-amber-400">{pendingCount} unsaved</span>
              <button
                onClick={handleDiscard}
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
        <div className={clsx("text-sm px-3 py-2 rounded", saveMsg.startsWith("Error") ? "bg-red-900/30 text-red-400" : "bg-emerald-900/30 text-emerald-400")}>
          {saveMsg}
        </div>
      )}

      {/* Tabs */}
      <div className="flex gap-1 border-b border-gray-800 overflow-x-auto">
        {TABS.map((tab) => {
          const tabChanged = tab.sections.some((s) => {
            const entries = getEntries(s);
            return entries.some(([k]) => k in edited);
          });
          return (
            <button
              key={tab.id}
              onClick={() => setActiveTab(tab.id)}
              className={clsx(
                "px-4 py-2.5 text-sm font-medium whitespace-nowrap border-b-2 transition-colors relative",
                activeTab === tab.id
                  ? "border-blue-500 text-blue-400"
                  : "border-transparent text-gray-500 hover:text-gray-300 hover:border-gray-700",
              )}
            >
              {tab.label}
              {tabChanged && <span className="absolute top-2 -right-0.5 w-1.5 h-1.5 rounded-full bg-amber-400" />}
            </button>
          );
        })}
      </div>

      {/* Tab content */}
      <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
        {currentTab.sections.map((sectionKey) => {
          const entries = getEntries(sectionKey);
          if (entries.length === 0) return null;
          const title = SECTION_LABELS[sectionKey] ?? sectionKey;
          return (
            <SectionCard
              key={sectionKey}
              title={title}
              entries={entries}
              edited={edited}
              onChange={handleChange}
            />
          );
        })}
      </div>
    </div>
  );
}
