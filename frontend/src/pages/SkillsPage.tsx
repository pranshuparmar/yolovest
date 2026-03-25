import { useState } from "react";
import { useListSkills, useRunSkill } from "../hooks/queries";
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
  const [runningSkill, setRunningSkill] = useState<string | null>(null);
  const [results, setResults] = useState<
    Record<string, { success: boolean; data?: Record<string, unknown>; error?: string | null }>
  >({});

  const handleRun = (skillName: string) => {
    setRunningSkill(skillName);
    setResults((prev) => {
      const next = { ...prev };
      delete next[skillName];
      return next;
    });
    runSkill.mutate(skillName, {
      onSuccess: (result) => {
        setResults((prev) => ({ ...prev, [skillName]: result }));
        setRunningSkill(null);
      },
      onError: (err) => {
        setResults((prev) => ({
          ...prev,
          [skillName]: { success: false, error: String(err) },
        }));
        setRunningSkill(null);
      },
    });
  };

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
          View all registered skills and manually trigger them.
        </p>
      </div>

      <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
        {skills?.map((skill) => {
          const result = results[skill.name];
          const isRunning = runningSkill === skill.name;

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
                <p className="text-[11px] text-gray-600 font-mono">
                  cron: {skill.schedule}
                </p>
              )}

              <button
                onClick={() => handleRun(skill.name)}
                disabled={isRunning}
                className="mt-auto px-3 py-1.5 rounded text-xs font-medium bg-gray-800 hover:bg-gray-700 text-gray-300 disabled:opacity-50 transition-colors border border-gray-700"
              >
                {isRunning ? "Running..." : "Run Now"}
              </button>

              {result && (
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
