import { useState } from "react";
import { NavLink, useLocation } from "react-router-dom";
import clsx from "clsx";

interface NavItem {
  to: string;
  label: string;
  icon: string;
}

interface NavGroup {
  label: string;
  items: NavItem[];
}

const groups: NavGroup[] = [
  {
    label: "Trading",
    items: [
      { to: "/", label: "Dashboard", icon: "D" },
      { to: "/positions", label: "Positions", icon: "P" },
      { to: "/trades", label: "Trades", icon: "T" },
      { to: "/watchlist", label: "Watchlist", icon: "W" },
      { to: "/holdings", label: "Holdings", icon: "H" },
      { to: "/dry-run", label: "Dry Run", icon: ">" },
      { to: "/alerts", label: "Alerts", icon: "!" },
    ],
  },
  {
    label: "Research",
    items: [
      { to: "/news", label: "News Feed", icon: "N" },
      { to: "/calendar", label: "Calendar", icon: "C" },
      { to: "/predictions", label: "Predictions", icon: "F" },
      { to: "/ml-models", label: "ML Models", icon: "M" },
    ],
  },
  {
    label: "Analysis",
    items: [
      { to: "/analytics", label: "Analytics", icon: "A" },
      { to: "/strategy", label: "Strategy", icon: "S" },
      { to: "/execution", label: "Execution", icon: "E" },
      { to: "/correlations", label: "Correlations", icon: "X" },
      { to: "/risk-sim", label: "Risk Sim", icon: "~" },
    ],
  },
  {
    label: "System",
    items: [
      { to: "/weekly", label: "Weekly", icon: "7" },
      { to: "/reports", label: "Reports", icon: "R" },
      { to: "/audit", label: "Audit Log", icon: "L" },
      { to: "/data", label: "Data Mgmt", icon: "B" },
      { to: "/skills", label: "Skills", icon: "K" },
      { to: "/integrations", label: "Settings", icon: "I" },
    ],
  },
];

function findActiveGroup(pathname: string): string {
  for (const g of groups) {
    for (const item of g.items) {
      if (item.to === "/" ? pathname === "/" : pathname.startsWith(item.to)) {
        return g.label;
      }
    }
  }
  return groups[0].label;
}

function GroupSection({
  group,
  collapsed,
  expanded,
  onToggle,
  onNavigate,
}: {
  group: NavGroup;
  collapsed: boolean;
  expanded: boolean;
  onToggle: () => void;
  onNavigate?: () => void;
}) {
  return (
    <div>
      {!collapsed && (
        <button
          onClick={onToggle}
          className="flex items-center justify-between w-full px-2.5 py-1.5 text-[10px] font-semibold uppercase tracking-wider text-gray-500 hover:text-gray-400"
        >
          <span>{group.label}</span>
          <svg
            className={clsx("w-3 h-3 transition-transform", expanded && "rotate-180")}
            fill="none"
            viewBox="0 0 24 24"
            stroke="currentColor"
          >
            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M19 9l-7 7-7-7" />
          </svg>
        </button>
      )}
      {(collapsed || expanded) && (
        <div className={clsx(!collapsed && "space-y-0.5")}>
          {group.items.map((link) => (
            <NavLink
              key={link.to}
              to={link.to}
              end={link.to === "/"}
              onClick={onNavigate}
              className={({ isActive }) =>
                clsx(
                  "flex items-center gap-2 px-2.5 py-1.5 rounded text-sm",
                  isActive
                    ? "bg-blue-900/30 text-blue-400"
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
        </div>
      )}
    </div>
  );
}

export function Sidebar({
  collapsed,
  onToggle,
  mobileOpen,
  onMobileClose,
}: {
  collapsed: boolean;
  onToggle: () => void;
  mobileOpen: boolean;
  onMobileClose: () => void;
}) {
  const location = useLocation();
  const activeGroup = findActiveGroup(location.pathname);

  // Track which groups are expanded (default: group containing active page)
  const [expandedGroups, setExpandedGroups] = useState<Record<string, boolean>>(() => {
    const init: Record<string, boolean> = {};
    for (const g of groups) {
      init[g.label] = g.label === activeGroup;
    }
    return init;
  });

  const toggleGroup = (label: string) => {
    setExpandedGroups((prev) => ({ ...prev, [label]: !prev[label] }));
  };

  const sidebarContent = (
    <aside
      className={clsx(
        "bg-gray-850 border-r border-gray-800 flex flex-col h-screen transition-all duration-200 shrink-0",
        // Desktop sizing
        "hidden md:flex",
        collapsed ? "md:w-14" : "md:w-52"
      )}
    >
      <div className="p-3 border-b border-gray-800 flex items-center justify-between">
        {!collapsed && (
          <div>
            <h1 className="text-base font-bold text-blue-400">YoloVest</h1>
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
      <nav className="flex-1 p-1.5 space-y-1 overflow-y-auto">
        {groups.map((group) => (
          <GroupSection
            key={group.label}
            group={group}
            collapsed={collapsed}
            expanded={expandedGroups[group.label] ?? false}
            onToggle={() => toggleGroup(group.label)}
          />
        ))}
      </nav>
    </aside>
  );

  // Mobile drawer overlay
  const mobileDrawer = (
    <>
      {/* Backdrop */}
      {mobileOpen && (
        <div
          className="fixed inset-0 bg-black/60 z-40 md:hidden"
          onClick={onMobileClose}
        />
      )}
      {/* Drawer */}
      <aside
        className={clsx(
          "fixed inset-y-0 left-0 z-50 w-64 bg-gray-900 border-r border-gray-800 flex flex-col transition-transform duration-200 md:hidden",
          mobileOpen ? "translate-x-0" : "-translate-x-full"
        )}
      >
        <div className="p-3 border-b border-gray-800 flex items-center justify-between">
          <div>
            <h1 className="text-base font-bold text-blue-400">YoloVest</h1>
            <p className="text-xs text-gray-500">Trading Dashboard</p>
          </div>
          <button
            onClick={onMobileClose}
            className="text-gray-500 hover:text-gray-300 p-1"
            title="Close menu"
          >
            <svg className="w-5 h-5" fill="none" viewBox="0 0 24 24" stroke="currentColor">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M6 18L18 6M6 6l12 12" />
            </svg>
          </button>
        </div>
        <nav className="flex-1 p-1.5 space-y-1 overflow-y-auto">
          {groups.map((group) => (
            <GroupSection
              key={group.label}
              group={group}
              collapsed={false}
              expanded={expandedGroups[group.label] ?? false}
              onToggle={() => toggleGroup(group.label)}
              onNavigate={onMobileClose}
            />
          ))}
        </nav>
      </aside>
    </>
  );

  return (
    <>
      {sidebarContent}
      {mobileDrawer}
    </>
  );
}
