import { exportToCSV } from "../utils/csvExport";

export function CSVExportButton({
  data,
  filename,
  label = "Export CSV",
}: {
  data: Record<string, unknown>[];
  filename: string;
  label?: string;
}) {
  if (!data || data.length === 0) return null;

  return (
    <button
      onClick={() => exportToCSV(data, filename)}
      className="px-3 py-1 text-xs bg-gray-800 border border-gray-700 rounded text-gray-300 hover:bg-gray-700 hover:text-gray-100 transition-colors"
    >
      {label}
    </button>
  );
}
