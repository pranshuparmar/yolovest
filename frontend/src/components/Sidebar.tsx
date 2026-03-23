import { NavLink } from "react-router-dom";
import clsx from "clsx";

const links = [
  { to: "/", label: "Dashboard", icon: "📊" },
  { to: "/positions", label: "Positions", icon: "📈" },
  { to: "/trades", label: "Trades", icon: "💹" },
  { to: "/watchlist", label: "Watchlist", icon: "👁" },
  { to: "/analytics", label: "Analytics", icon: "🔬" },
  { to: "/reports", label: "Reports", icon: "📄" },
  { to: "/audit", label: "Audit Log", icon: "📋" },
];

export function Sidebar() {
  return (
    <aside className="w-56 bg-gray-900 border-r border-gray-800 flex flex-col min-h-screen">
      <div className="p-4 border-b border-gray-800">
        <h1 className="text-lg font-bold text-emerald-400">YoloVest</h1>
        <p className="text-xs text-gray-500">Trading Dashboard</p>
      </div>
      <nav className="flex-1 p-2 space-y-1">
        {links.map((link) => (
          <NavLink
            key={link.to}
            to={link.to}
            end={link.to === "/"}
            className={({ isActive }) =>
              clsx(
                "flex items-center gap-2 px-3 py-2 rounded text-sm",
                isActive
                  ? "bg-emerald-900/40 text-emerald-400"
                  : "text-gray-400 hover:bg-gray-800 hover:text-gray-200"
              )
            }
          >
            <span>{link.icon}</span>
            {link.label}
          </NavLink>
        ))}
      </nav>
    </aside>
  );
}
