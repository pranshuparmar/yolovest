import { useEffect, useRef, useState } from "react";
import clsx from "clsx";
import { useKillSwitch, useSystemState } from "../hooks/queries";

type Command = "pause" | "stop" | "kill" | "resume";

interface PendingConfirm {
  command: Command;
  label: string;
  message: string;
}

const CONFIRMS: Record<Command, { label: string; message: string }> = {
  pause: {
    label: "Pause trading (soft)",
    message:
      "Block new signals from executing. Every existing broker order, GTT, SL leg and position is left untouched — exactly as it is right now. Use this when you want the bot to stop taking new bets but keep current protections alive. Continue?",
  },
  stop: {
    label: "Stop trading",
    message:
      "Cancel all pending orders (including SL / target legs of open MIS positions) and pause new signal execution. Open positions stay open but may lose their stop-loss protection. Continue?",
  },
  kill: {
    label: "Kill (square off + pause)",
    message:
      "Cancel pending orders, square off EVERY open position at market (including CNC), and pause trading. This is irreversible for the closed positions. Continue?",
  },
  resume: {
    label: "Resume trading",
    message:
      "Clear the kill switch and allow new signals to execute. Continue?",
  },
};

const ACTIVE_LABELS: Record<"pause" | "stop" | "kill", string> = {
  pause: "Paused",
  stop: "Stopped",
  kill: "Killed",
};

export function KillSwitchControl() {
  const { data: systemState } = useSystemState();
  const killSwitch = useKillSwitch();
  const [open, setOpen] = useState(false);
  const [confirm, setConfirm] = useState<PendingConfirm | null>(null);
  const wrapperRef = useRef<HTMLDivElement | null>(null);

  const active = Boolean(systemState?.kill_switch_active);
  const mode = systemState?.kill_switch_mode || "";
  const activeLabel =
    mode && mode in ACTIVE_LABELS
      ? ACTIVE_LABELS[mode as "pause" | "stop" | "kill"]
      : "Paused";

  useEffect(() => {
    if (!open) return;
    const onDocClick = (e: MouseEvent) => {
      if (!wrapperRef.current?.contains(e.target as Node)) {
        setOpen(false);
      }
    };
    document.addEventListener("mousedown", onDocClick);
    return () => document.removeEventListener("mousedown", onDocClick);
  }, [open]);

  const trigger = (command: Command) => {
    const meta = CONFIRMS[command];
    setConfirm({ command, label: meta.label, message: meta.message });
    setOpen(false);
  };

  const execute = () => {
    if (!confirm) return;
    killSwitch.mutate(confirm.command, {
      onSettled: () => setConfirm(null),
    });
  };

  return (
    <div ref={wrapperRef} className="relative">
      <button
        onClick={() => setOpen((v) => !v)}
        title={active ? "Trading paused — click to resume" : "Kill switch controls"}
        className={clsx(
          "flex items-center gap-1.5 text-xs px-2 py-1 rounded-full border transition-colors",
          active
            ? "border-red-700 bg-red-900/30 text-red-300 hover:bg-red-900/50"
            : "border-gray-700 text-gray-300 hover:bg-gray-800",
        )}
      >
        <span
          className={clsx(
            "w-2 h-2 rounded-full",
            active ? "bg-red-400 animate-pulse" : "bg-gray-500",
          )}
        />
        <span className="hidden sm:inline">
          {active ? activeLabel : "Kill switch"}
        </span>
      </button>

      {open && (
        <div className="absolute right-0 top-full mt-2 w-56 rounded-md border border-gray-700 bg-gray-900 shadow-lg z-50 p-1.5">
          {active ? (
            <button
              onClick={() => trigger("resume")}
              className="w-full text-left px-2 py-1.5 rounded hover:bg-emerald-900/40 text-sm text-emerald-300"
            >
              Resume trading
            </button>
          ) : (
            <>
              <button
                onClick={() => trigger("pause")}
                className="w-full text-left px-2 py-1.5 rounded hover:bg-sky-900/40 text-sm text-sky-300"
                title="Block new trades only. Existing orders, GTTs and positions are untouched."
              >
                Pause (block new trades, broker untouched)
              </button>
              <button
                onClick={() => trigger("stop")}
                className="w-full text-left px-2 py-1.5 rounded hover:bg-amber-900/40 text-sm text-amber-300"
                title="Pause + cancel every pending broker order, including SL/target legs."
              >
                Stop (cancel orders, keep positions)
              </button>
              <button
                onClick={() => trigger("kill")}
                className="w-full text-left px-2 py-1.5 rounded hover:bg-red-900/40 text-sm text-red-300"
                title="Pause + cancel orders + square off everything at market."
              >
                Kill (square off everything)
              </button>
            </>
          )}
          <div className="border-t border-gray-800 mt-1.5 pt-1.5 px-2 text-[10px] text-gray-500 leading-snug">
            Mirrors /pause /stop /kill /resume in Telegram. Persists across restarts.
          </div>
        </div>
      )}

      {confirm && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60">
          <div className="w-full max-w-md rounded-lg border border-gray-700 bg-gray-900 p-5 shadow-xl">
            <h3 className="text-base font-semibold text-gray-100 mb-2">
              {confirm.label}
            </h3>
            <p className="text-sm text-gray-300 mb-4">{confirm.message}</p>
            <div className="flex items-center justify-end gap-2">
              <button
                onClick={() => setConfirm(null)}
                disabled={killSwitch.isPending}
                className="px-3 py-1.5 rounded text-sm bg-gray-700 hover:bg-gray-600 text-gray-200 disabled:opacity-50"
              >
                Cancel
              </button>
              <button
                onClick={execute}
                disabled={killSwitch.isPending}
                className={clsx(
                  "px-3 py-1.5 rounded text-sm text-white disabled:opacity-50",
                  confirm.command === "resume"
                    ? "bg-emerald-600 hover:bg-emerald-700"
                    : confirm.command === "kill"
                      ? "bg-red-600 hover:bg-red-700"
                      : "bg-amber-600 hover:bg-amber-700",
                )}
              >
                {killSwitch.isPending ? "Working…" : "Confirm"}
              </button>
            </div>
            {killSwitch.isError && (
              <p className="mt-3 text-xs text-red-400">
                {(killSwitch.error as Error)?.message || "Request failed"}
              </p>
            )}
          </div>
        </div>
      )}
    </div>
  );
}
