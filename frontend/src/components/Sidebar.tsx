import { NavLink } from "react-router-dom";
import clsx from "clsx";

const links = [
  { to: "/", label: "Dashboard", icon: "D" },
  { to: "/positions", label: "Positions", icon: "P" },
  { to: "/trades", label: "Trades", icon: "T" },
  { to: "/watchlist", label: "Watchlist", icon: "W" },
  { to: "/alerts", label: "Alerts", icon: "!" },
  { to: "/news", label: "News Feed", icon: "N" },
  { to: "/calendar", label: "Calendar", icon: "C" },
  { to: "/predictions", label: "Predictions", icon: "F" },
  { to: "/ml-models", label: "ML Models", icon: "M" },
  { to: "/strategy", label: "Strategy", icon: "S" },
  { to: "/execution", label: "Execution", icon: "E" },
  { to: "/correlations", label: "Correlations", icon: "X" },
  { to: "/risk-sim", label: "Risk Sim", icon: "~" },
  { to: "/analytics", label: "Analytics", icon: "A" },
  { to: "/weekly", label: "Weekly", icon: "7" },
  { to: "/reports", label: "Reports", icon: "R" },
  { to: "/audit", label: "Audit Log", icon: "L" },
  { to: "/data", label: "Data Mgmt", icon: "B" },
  { to: "/integrations", label: "Integrations", icon: "I" },
];

export function Sidebar({
  collapsed,
  onToggle,
}: {
  collapsed: boolean;
  onToggle: () => void;
}) {
  return (
    <aside
      className={clsx(
        "bg-gray-900 border-r border-gray-800 flex flex-col h-screen sticky top-0 transition-all duration-200 shrink-0",
        collapsed ? "w-14" : "w-52"
      )}
    >
      <div className="p-3 border-b border-gray-800 flex items-center justify-between">
        {!collapsed && (
          <div>
            <h1 className="text-base font-bold text-emerald-400">YoloVest</h1>
            <p className="text-xs text-gray-500">Trading Dashboard</p>
          </div>
        )}
        <button
          onClick={onToggle}
          className="text-gray-500 hover:text-gray-300 p-1"
          title={collapsed ? "Expand sidebar" : "Collapse sidebar"}
        >
          <svg className="w-4 h-4" fill="none" viewBox="0 0 24 24" stroke="currentColor">
            {collapsed ? (
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M13 5l7 7-7 7M5 5l7 7-7 7" />
            ) : (
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M11 19l-7-7 7-7m8 14l-7-7 7-7" />
            )}
          </svg>
        </button>
      </div>
      <nav className="flex-1 p-1.5 space-y-0.5 overflow-y-auto">
        {links.map((link) => (
          <NavLink
            key={link.to}
            to={link.to}
            end={link.to === "/"}
            className={({ isActive }) =>
              clsx(
                "flex items-center gap-2 px-2.5 py-1.5 rounded text-sm",
                isActive
                  ? "bg-emerald-900/40 text-emerald-400"
                  : "text-gray-400 hover:bg-gray-800 hover:text-gray-200"
              )
            }
            title={collapsed ? link.label : undefined}
          >
            <span className="w-5 h-5 rounded bg-gray-800/50 flex items-center justify-center text-xs font-bold shrink-0">
              {link.icon}
            </span>
            {!collapsed && <span>{link.label}</span>}
          </NavLink>
        ))}
      </nav>
    </aside>
  );
}
