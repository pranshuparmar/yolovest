import { useState, useEffect, useCallback, useRef } from "react";
import { useQueryClient } from "@tanstack/react-query";
import clsx from "clsx";

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

  useEffect(() => {
    function connect() {
      const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
      const ws = new WebSocket(`${protocol}//${window.location.host}/ws`);
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
          } else {
            addNotification(type, JSON.stringify(data).slice(0, 100));
          }
        } catch {
          // ignore malformed
        }
      };

      ws.onclose = () => {
        const delay = Math.min(1000 * 2 ** retryRef.current, 30000);
        retryRef.current++;
        setTimeout(connect, delay);
      };

      ws.onerror = () => ws.close();
    }

    connect();
    return () => wsRef.current?.close();
  }, [queryClient, addNotification]);

  return { notifications, clearAll, dismiss };
}

const typeIcons: Record<string, string> = {
  trade: "T",
  position: "P",
  report: "R",
  signal: "S",
  prediction: "?",
};

const typeColors: Record<string, string> = {
  trade: "bg-emerald-900/40 text-emerald-400",
  position: "bg-blue-900/40 text-blue-400",
  report: "bg-purple-900/40 text-purple-400",
  signal: "bg-amber-900/40 text-amber-400",
  prediction: "bg-cyan-900/40 text-cyan-400",
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

  return (
    <div className="relative">
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
          <span className="absolute -top-1 -right-1 w-4 h-4 bg-emerald-500 text-white text-xs rounded-full flex items-center justify-center">
            {unread > 9 ? "9+" : unread}
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
                      {n.timestamp.toLocaleTimeString("en-IN")}
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
