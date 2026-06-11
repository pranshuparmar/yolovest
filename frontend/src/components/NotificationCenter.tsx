import { useState, useEffect, useCallback, useRef } from "react";
import { useQueryClient } from "@tanstack/react-query";
import clsx from "clsx";
import { getTimezone } from "../utils/datetime";
import { feedTick } from "../hooks/useLtpStream";

interface Notification {
  id: number;
  type: string;
  message: string;
  timestamp: Date;
}

let _nextId = 1;

export function useNotifications() {
  const [notifications, setNotifications] = useState<Notification[]>([]);
  const queryClient = useQueryClient();
  const wsRef = useRef<WebSocket | null>(null);
  const retryRef = useRef(0);

  const addNotification = useCallback((type: string, message: string) => {
    setNotifications((prev) => [
      { id: _nextId++, type, message, timestamp: new Date() },
      ...prev.slice(0, 49),
    ]);
  }, []);

  const clearAll = useCallback(() => setNotifications([]), []);
  const dismiss = useCallback(
    (id: number) =>
      setNotifications((prev) => prev.filter((n) => n.id !== id)),
    []
  );

  // Mutation failures dispatched by the global MutationCache (App.tsx).
  useEffect(() => {
    const onMutationError = (e: Event) => {
      const detail = (e as CustomEvent<{ message?: string }>).detail;
      addNotification("alert", `Action failed: ${detail?.message ?? "unknown error"}`);
    };
    window.addEventListener("yolovest-mutation-error", onMutationError);
    return () => window.removeEventListener("yolovest-mutation-error", onMutationError);
  }, [addNotification]);

  useEffect(() => {
    let disposed = false;
    function connect() {
      if (disposed) return;
      const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
      // The browser WebSocket API can't set an Authorization header, so
      // the session token rides as a query param. Read it at connect
      // time (not mount time) so a reconnect after re-login picks up
      // the fresh token.
      const token = localStorage.getItem("yv_token") ?? "";
      const ws = new WebSocket(
        `${protocol}//${window.location.host}/ws?token=${encodeURIComponent(token)}`
      );
      wsRef.current = ws;

      ws.onopen = () => {
        retryRef.current = 0;
      };

      ws.onmessage = (event) => {
        try {
          const msg = JSON.parse(event.data);
          const type = msg.type as string;
          const data = msg.data || {};

          if (type === "trade_executed" || type === "trade_entry") {
            addNotification(
              "trade",
              `Trade executed: ${data.symbol || "Unknown"} ${data.signal_type || ""}`
            );
            queryClient.invalidateQueries({ queryKey: ["positions"] });
            queryClient.invalidateQueries({ queryKey: ["trades"] });
            queryClient.invalidateQueries({ queryKey: ["portfolio"] });
            queryClient.invalidateQueries({ queryKey: ["risk-exposure"] });
          } else if (type === "trade_exit") {
            addNotification(
              "trade",
              `Trade closed: ${data.symbol || "Unknown"} PnL: ${data.pnl ?? "—"}`
            );
            queryClient.invalidateQueries({ queryKey: ["positions"] });
            queryClient.invalidateQueries({ queryKey: ["trades"] });
            queryClient.invalidateQueries({ queryKey: ["portfolio"] });
            queryClient.invalidateQueries({ queryKey: ["risk-exposure"] });
          } else if (type === "position_updated") {
            addNotification(
              "position",
              `Position updated: ${data.symbol || "Unknown"}`
            );
            queryClient.invalidateQueries({ queryKey: ["positions"] });
            queryClient.invalidateQueries({ queryKey: ["portfolio"] });
          } else if (type === "report_generated") {
            addNotification(
              "report",
              `Report generated: ${data.report_type || "Unknown"}`
            );
            queryClient.invalidateQueries({ queryKey: ["reports"] });
          } else if (type === "signal_generated") {
            addNotification(
              "signal",
              `Signal: ${data.symbol || "Unknown"} ${data.signal_type || ""}`
            );
          } else if (type === "prediction_scored") {
            addNotification(
              "prediction",
              `Prediction scored: ${data.symbol || "Unknown"}`
            );
            queryClient.invalidateQueries({ queryKey: ["predictions"] });
            queryClient.invalidateQueries({ queryKey: ["scoreboard"] });
          } else if (type === "skill_completed") {
            const status = data.success ? "completed" : "failed";
            const dur = data.duration_ms ? ` (${(data.duration_ms / 1000).toFixed(1)}s)` : "";
            addNotification(
              "skill",
              `${data.skill || "Unknown"} ${status}${dur}`
            );
            queryClient.invalidateQueries({ queryKey: ["watchlist"] });
            queryClient.invalidateQueries({ queryKey: ["ml-models"] });
            queryClient.invalidateQueries({ queryKey: ["storage-stats"] });
            // Notify SkillsPage to update running state
            window.dispatchEvent(new CustomEvent("yolovest-skill-completed", { detail: data }));
          } else if (type === "heartbeat_started") {
            addNotification("heartbeat", "Heartbeat started");
          } else if (type === "heartbeat_completed") {
            const n = data.signals_generated || 0;
            addNotification(
              "heartbeat",
              `Heartbeat done: ${data.skills_succeeded}/${data.skills_run} skills, ${n} signals`
            );
            queryClient.invalidateQueries({ queryKey: ["portfolio"] });
            queryClient.invalidateQueries({ queryKey: ["positions"] });
          } else if (type === "kill_switch_activated") {
            addNotification(
              "alert",
              `Kill switch: ${data.command?.toUpperCase()}${data.total_pnl != null ? ` PnL: ${data.total_pnl}` : ""}`
            );
            queryClient.invalidateQueries({ queryKey: ["portfolio"] });
            queryClient.invalidateQueries({ queryKey: ["positions"] });
          } else if (type === "portfolio_pnl") {
            if (data.targets_hit > 0 || data.stops_hit > 0) {
              addNotification(
                "position",
                `Positions: ${data.positions} open, ${data.targets_hit} targets hit, ${data.stops_hit} SL hit`
              );
            }
            queryClient.invalidateQueries({ queryKey: ["portfolio"] });
            queryClient.invalidateQueries({ queryKey: ["positions"] });
          } else if (type === "ingest_progress") {
            // Only show final progress to avoid spam
            if (data.current === data.total) {
              addNotification(
                "skill",
                `${data.skill}: ${data.total} symbols ingested`
              );
            }
          } else if (type === "retrain_progress") {
            if (data.status === "completed") {
              addNotification(
                "skill",
                `${data.model_type} model trained: Sharpe ${data.sharpe?.toFixed(2) ?? "?"}`
              );
              queryClient.invalidateQueries({ queryKey: ["ml-models"] });
            }
          } else if (type === "order_update") {
            // Broker order state changed (postback or KiteTicker order frame).
            // Refresh anything that depends on order state so the UI doesn't
            // need a manual reload after fills/cancels/rejects.
            const status = (data.status || "").toString().toUpperCase();
            if (status === "REJECTED") {
              addNotification(
                "alert",
                `Order REJECTED: ${data.symbol || "?"} ${data.transaction_type || ""} (${data.order_id || "?"})`
              );
            }
            queryClient.invalidateQueries({ queryKey: ["positions"] });
            queryClient.invalidateQueries({ queryKey: ["trades"] });
            queryClient.invalidateQueries({ queryKey: ["trade-detail"] });
            queryClient.invalidateQueries({ queryKey: ["portfolio"] });
          } else if (type === "pending_approved" || type === "pending_rejected" || type === "pending_expired") {
            const action = type.replace("pending_", "");
            addNotification(
              type === "pending_expired" ? "alert" : "trade",
              `${data.symbol || "?"} pending ${action}`
            );
            queryClient.invalidateQueries({ queryKey: ["pending-trades"] });
            queryClient.invalidateQueries({ queryKey: ["recommendations"] });
          } else if (type === "heartbeat_stage") {
            // Per-skill progress within a heartbeat — surface on Skills page
            // and as a low-priority chip. No notification to avoid spam.
            window.dispatchEvent(new CustomEvent("yolovest-heartbeat-stage", { detail: data }));
          } else if (type === "broker_auth_lost") {
            addNotification(
              "alert",
              `Broker auth lost — re-auth required via /auth or Integrations page`
            );
            queryClient.invalidateQueries({ queryKey: ["integrations-status"] });
          } else if (type === "tick_update") {
            // High-frequency frame: feed the LTP store and skip the
            // toast/invalidate path so we don't drown the user.
            feedTick(data.symbol, data.ltp);
          } else {
            addNotification(type, JSON.stringify(data).slice(0, 100));
          }
        } catch {
          // ignore malformed
        }
      };

      ws.onclose = () => {
        if (disposed) return;
        const delay = Math.min(1000 * 2 ** retryRef.current, 30000);
        retryRef.current++;
        setTimeout(connect, delay);
      };

      ws.onerror = () => ws.close();
    }

    connect();
    return () => {
      disposed = true;
      wsRef.current?.close();
    };
  }, [queryClient, addNotification]);

  return { notifications, clearAll, dismiss };
}

const typeIcons: Record<string, string> = {
  trade: "T",
  position: "P",
  report: "R",
  signal: "S",
  prediction: "?",
  skill: "K",
  heartbeat: "H",
  alert: "!",
};

const typeColors: Record<string, string> = {
  trade: "bg-emerald-900/40 text-emerald-400",
  position: "bg-blue-900/40 text-blue-400",
  report: "bg-purple-900/40 text-purple-400",
  signal: "bg-amber-900/40 text-amber-400",
  prediction: "bg-cyan-900/40 text-cyan-400",
  skill: "bg-blue-900/40 text-blue-400",
  heartbeat: "bg-gray-800 text-gray-400",
  alert: "bg-red-900/40 text-red-400",
};

export function NotificationCenter({
  notifications,
  clearAll,
  dismiss,
}: {
  notifications: Notification[];
  clearAll: () => void;
  dismiss: (id: number) => void;
}) {
  const [open, setOpen] = useState(false);
  const unread = notifications.length;
  const containerRef = useRef<HTMLDivElement>(null);

  // Close on outside click
  useEffect(() => {
    if (!open) return;
    function handleClick(e: MouseEvent) {
      if (containerRef.current && !containerRef.current.contains(e.target as Node)) {
        setOpen(false);
      }
    }
    document.addEventListener("mousedown", handleClick);
    return () => document.removeEventListener("mousedown", handleClick);
  }, [open]);

  return (
    <div className="relative" ref={containerRef}>
      <button
        onClick={() => setOpen(!open)}
        className="relative text-gray-400 hover:text-gray-200 p-1"
      >
        <svg
          className="w-5 h-5"
          fill="none"
          viewBox="0 0 24 24"
          stroke="currentColor"
        >
          <path
            strokeLinecap="round"
            strokeLinejoin="round"
            strokeWidth={2}
            d="M15 17h5l-1.405-1.405A2.032 2.032 0 0118 14.158V11a6.002 6.002 0 00-4-5.659V5a2 2 0 10-4 0v.341C7.67 6.165 6 8.388 6 11v3.159c0 .538-.214 1.055-.595 1.436L4 17h5m6 0v1a3 3 0 11-6 0v-1m6 0H9"
          />
        </svg>
        {unread > 0 && (
          <span className="absolute -top-0.5 -right-0.5 min-w-[16px] h-4 px-1 bg-blue-500 text-white text-[10px] font-medium leading-4 rounded-full flex items-center justify-center">
            {unread > 99 ? "99" : unread}
          </span>
        )}
      </button>

      {open && (
        <div className="absolute right-0 mt-2 w-80 bg-gray-900 border border-gray-800 rounded-lg shadow-xl z-50 max-h-96 flex flex-col">
          <div className="flex items-center justify-between px-3 py-2 border-b border-gray-800">
            <span className="text-xs font-medium text-gray-400">
              Notifications
            </span>
            {notifications.length > 0 && (
              <button
                onClick={clearAll}
                className="text-xs text-gray-500 hover:text-gray-300"
              >
                Clear all
              </button>
            )}
          </div>
          <div className="overflow-y-auto flex-1">
            {notifications.length === 0 ? (
              <p className="text-gray-500 text-xs py-6 text-center">
                No notifications
              </p>
            ) : (
              notifications.map((n) => (
                <div
                  key={n.id}
                  className="flex items-start gap-2 px-3 py-2 border-b border-gray-800/50 hover:bg-gray-800/30 last:border-0"
                >
                  <span
                    className={clsx(
                      "w-5 h-5 rounded flex items-center justify-center text-xs font-bold shrink-0 mt-0.5",
                      typeColors[n.type] || "bg-gray-800 text-gray-400"
                    )}
                  >
                    {typeIcons[n.type] || "?"}
                  </span>
                  <div className="flex-1 min-w-0">
                    <p className="text-xs text-gray-300 break-words">
                      {n.message}
                    </p>
                    <p className="text-xs text-gray-600 mt-0.5">
                      {n.timestamp.toLocaleTimeString("en-IN", { timeZone: getTimezone() })}
                    </p>
                  </div>
                  <button
                    onClick={() => dismiss(n.id)}
                    className="text-gray-600 hover:text-gray-400 shrink-0"
                  >
                    x
                  </button>
                </div>
              ))
            )}
          </div>
        </div>
      )}
    </div>
  );
}
