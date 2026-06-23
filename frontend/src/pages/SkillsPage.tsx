import { useState, useEffect, useCallback } from "react";
import { useListSkills, useRunSkill, useSetScheduleEnabled } from "../hooks/queries";
import { formatIST } from "../utils/datetime";
import clsx from "clsx";

const TRIGGER_COLORS: Record<string, string> = {
  heartbeat: "bg-blue-900/40 text-blue-400 border-blue-800",
  cron: "bg-purple-900/40 text-purple-400 border-purple-800",
  event: "bg-amber-900/40 text-amber-400 border-amber-800",
  manual: "bg-emerald-900/40 text-emerald-400 border-emerald-800",
};

export function SkillsPage() {
  const { data: skills, isLoading } = useListSkills();
  const runSkill = useRunSkill();
  const toggleSchedule = useSetScheduleEnabled();
  const [runningSkills, setRunningSkills] = useState<Set<string>>(new Set());
  const [results, setResults] = useState<
    Record<string, { success: boolean; data?: Record<string, unknown>; error?: string | null; status?: string }>
  >({});

  // Listen for WebSocket skill_completed events to clear running state
  useEffect(() => {
    function handleWsMessage(event: MessageEvent) {
      try {
        const msg = JSON.parse(event.data);
        if (msg.type === "skill_completed" && msg.data?.skill) {
          const name = msg.data.skill as string;
          setRunningSkills((prev) => {
            const next = new Set(prev);
            next.delete(name);
            return next;
          });
          setResults((prev) => ({
            ...prev,
            [name]: {
              success: msg.data.success,
              error: msg.data.error,
              data: msg.data.data,
              status: "completed",
            },
          }));
        }
      } catch {
        // ignore
      }
    }

    // Find existing WebSocket connection
    // The NotificationCenter creates the WS — we add a listener to the same connection
    // Use a BroadcastChannel to share events between components
    const bc = new BroadcastChannel("yolovest-ws");
    bc.onmessage = (event) => handleWsMessage(event as unknown as MessageEvent);

    // Also listen on window for direct WS messages forwarded from NotificationCenter
    window.addEventListener("yolovest-skill-completed", ((e: CustomEvent) => {
      const data = e.detail;
      if (data?.skill) {
        setRunningSkills((prev) => {
          const next = new Set(prev);
          next.delete(data.skill);
          return next;
        });
        setResults((prev) => ({
          ...prev,
          [data.skill]: {
            success: data.success,
            error: data.error,
            data: data.data || {},
            status: "completed",
          },
        }));
      }
    }) as EventListener);

    return () => bc.close();
  }, []);

  const handleRun = useCallback((skillName: string) => {
    setRunningSkills((prev) => new Set(prev).add(skillName));
    setResults((prev) => {
      const next = { ...prev };
      delete next[skillName];
      return next;
    });
    runSkill.mutate(skillName, {
      onSuccess: (result) => {
        const status = (result as Record<string, unknown>).status as string | undefined;
        if (status === "started" || status === "already_running") {
          setResults((prev) => ({
            ...prev,
            [skillName]: { success: true, data: { status }, status: status },
          }));
        } else {
          setResults((prev) => ({ ...prev, [skillName]: { ...result, status: "completed" } }));
          setRunningSkills((prev) => {
            const next = new Set(prev);
            next.delete(skillName);
            return next;
          });
        }
      },
      onError: (err) => {
        setResults((prev) => ({
          ...prev,
          [skillName]: { success: false, error: String(err), status: "failed" },
        }));
        setRunningSkills((prev) => {
          const next = new Set(prev);
          next.delete(skillName);
          return next;
        });
      },
    });
  }, [runSkill]);

  if (isLoading) {
    return (
      <div className="flex items-center justify-center h-64 text-gray-500">
        Loading skills...
      </div>
    );
  }

  return (
    <div className="space-y-6">
      <div>
        <h2 className="text-lg font-bold text-gray-100">Skills</h2>
        <p className="text-sm text-gray-500 mt-1">
          View all registered skills, manually trigger them, and start/stop
          scheduled (CRON) skills.
        </p>
      </div>

      <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
        {skills?.map((skill) => {
          const result = results[skill.name];
          const isRunning = runningSkills.has(skill.name);

          return (
            <div
              key={skill.name}
              className="bg-gray-900 border border-gray-800 rounded-lg p-4 flex flex-col gap-3"
            >
              <div className="flex items-start justify-between gap-2">
                <div className="min-w-0">
                  <h3 className="text-sm font-semibold text-gray-100 font-mono truncate">
                    {skill.name}
                  </h3>
                  <p className="text-xs text-gray-500 mt-1">
                    {skill.description}
                  </p>
                </div>
                <span
                  className={clsx(
                    "text-[10px] font-medium px-2 py-0.5 rounded border shrink-0 uppercase tracking-wide",
                    TRIGGER_COLORS[skill.trigger] ??
                      "bg-gray-800 text-gray-400 border-gray-700",
                  )}
                >
                  {skill.trigger}
                </span>
              </div>

              {skill.schedule && (
                <div className="text-[11px] font-mono space-y-0.5">
                  <p className="text-gray-600">cron: {skill.schedule}</p>
                  {skill.enabled === false ? (
                    <p className="text-amber-500/90">Schedule paused</p>
                  ) : skill.next_run ? (
                    <p className="text-gray-600">next run: {formatIST(skill.next_run)}</p>
                  ) : null}
                </div>
              )}

              {/* Stop/Start schedule sits inline with Run Now as a split
                  control so the two actions share one row instead of
                  stacking and eating vertical space. */}
              <div className="mt-auto flex gap-2">
                {/* CRON skills (enabled is bool, null for non-cron) can be
                    started/stopped — pauses only the auto-fire, not Run Now. */}
                {skill.enabled !== null && (
                  <button
                    onClick={() =>
                      toggleSchedule.mutate({ skillName: skill.name, enabled: !skill.enabled })
                    }
                    disabled={toggleSchedule.isPending}
                    className={clsx(
                      "flex-1 px-3 py-1.5 rounded text-xs font-medium transition-colors border disabled:opacity-50 whitespace-nowrap",
                      skill.enabled
                        ? "bg-amber-900/20 hover:bg-amber-900/40 text-amber-400 border-amber-800"
                        : "bg-emerald-900/20 hover:bg-emerald-900/40 text-emerald-400 border-emerald-800",
                    )}
                    title={skill.enabled
                      ? "Pause this schedule — it won't auto-fire (manual Run Now still works)"
                      : "Resume this schedule"}
                  >
                    {skill.enabled ? "■ Stop schedule" : "▶ Start schedule"}
                  </button>
                )}
                {isRunning ? (
                  <div className="flex-1 flex items-center justify-center gap-2 px-3 py-1.5 rounded text-xs font-medium bg-amber-900/20 border border-amber-800 text-amber-400">
                    <div className="w-3 h-3 border-2 border-amber-800 border-t-amber-400 rounded-full animate-spin" />
                    Running...
                  </div>
                ) : (
                  <button
                    onClick={() => handleRun(skill.name)}
                    className="flex-1 px-3 py-1.5 rounded text-xs font-medium bg-gray-800 hover:bg-gray-700 text-gray-300 transition-colors border border-gray-700"
                  >
                    Run Now
                  </button>
                )}
              </div>

              {result && result.status === "completed" && (
                <div
                  className={clsx(
                    "rounded p-2 text-xs",
                    result.success
                      ? "bg-emerald-900/20 border border-emerald-800 text-emerald-400"
                      : "bg-red-900/20 border border-red-800 text-red-400",
                  )}
                >
                  {result.success ? "Completed" : "Failed"}
                  {result.error && <span> — {result.error}</span>}
                  {result.data && Object.keys(result.data).length > 0 && (
                    <pre className="mt-1 text-[10px] text-gray-500 overflow-x-auto whitespace-pre-wrap">
                      {JSON.stringify(result.data, null, 2)}
                    </pre>
                  )}
                </div>
              )}
            </div>
          );
        })}
      </div>
    </div>
  );
}
