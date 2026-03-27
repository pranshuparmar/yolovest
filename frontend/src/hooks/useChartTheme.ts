import { useTheme } from "./useTheme";

interface ChartTheme {
  grid: string;
  tick: string;
  tooltipBg: string;
  tooltipBorder: string;
  tooltipText: string;
}

const dark: ChartTheme = {
  grid: "#21262d",
  tick: "#8b949e",
  tooltipBg: "#161b22",
  tooltipBorder: "#30363d",
  tooltipText: "#e6edf3",
};

const light: ChartTheme = {
  grid: "#d0d7de",
  tick: "#656d76",
  tooltipBg: "#ffffff",
  tooltipBorder: "#d0d7de",
  tooltipText: "#1f2328",
};

export function useChartTheme(): ChartTheme {
  const { theme } = useTheme();
  return theme === "light" ? light : dark;
}

export function useTooltipStyle() {
  const t = useChartTheme();
  return {
    backgroundColor: t.tooltipBg,
    border: `1px solid ${t.tooltipBorder}`,
    borderRadius: 8,
    color: t.tooltipText,
    fontSize: "12px",
  } as const;
}
