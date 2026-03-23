import { useState } from "react";
import { ReportViewer } from "../components/ReportViewer";
import { useReports } from "../hooks/queries";

export function ReportsPage() {
  const [reportType, setReportType] = useState<string | undefined>(undefined);
  const [start, setStart] = useState("");
  const [end, setEnd] = useState("");

  const { data, isLoading } = useReports({
    report_type: reportType,
    start: start || undefined,
    end: end || undefined,
  });

  return (
    <div className="space-y-6">
      <h2 className="text-lg font-semibold">Reports</h2>

      <div className="flex flex-wrap gap-3 items-end">
        <div>
          <label className="block text-xs text-gray-500 mb-1">Type</label>
          <select
            value={reportType || ""}
            onChange={(e) => setReportType(e.target.value || undefined)}
            className="bg-gray-800 border border-gray-700 rounded px-2 py-1.5 text-sm text-gray-100"
          >
            <option value="">All</option>
            <option value="daily">Daily</option>
            <option value="weekly">Weekly</option>
          </select>
        </div>
        <div>
          <label className="block text-xs text-gray-500 mb-1">Start Date</label>
          <input
            type="date"
            value={start}
            onChange={(e) => setStart(e.target.value)}
            className="bg-gray-800 border border-gray-700 rounded px-2 py-1.5 text-sm text-gray-100"
          />
        </div>
        <div>
          <label className="block text-xs text-gray-500 mb-1">End Date</label>
          <input
            type="date"
            value={end}
            onChange={(e) => setEnd(e.target.value)}
            className="bg-gray-800 border border-gray-700 rounded px-2 py-1.5 text-sm text-gray-100"
          />
        </div>
      </div>

      {isLoading ? (
        <div className="h-40 animate-pulse bg-gray-900 rounded-lg" />
      ) : (
        <ReportViewer reports={data || []} />
      )}
    </div>
  );
}
