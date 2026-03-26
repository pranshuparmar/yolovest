/** @type {import('tailwindcss').Config} */
export default {
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        gray: {
          950: "#000000", // pure black page background
          900: "#111111", // card/panel background (subtle lift from black)
          800: "#1e1e1e", // borders, elevated surfaces
          700: "#2d2d2d", // subtle hover, dividers
          600: "#555555", // muted UI elements
          500: "#777777", // comments, tertiary text
          400: "#999999", // secondary text
          300: "#bbbbbb", // stronger secondary text
          200: "#dddddd", // primary text
          100: "#f0f0f0", // headings, bright text
        },
        emerald: {
          400: "#34d399", // vivid green — BUY, success
          500: "#10b981",
          600: "#059669",
          700: "#047857",
          800: "#065f46",
          900: "#064e3b",
        },
        red: {
          400: "#f87171", // vivid red — SELL, error
          500: "#ef4444",
          600: "#dc2626",
          800: "#991b1b",
          900: "#7f1d1d",
        },
        amber: {
          400: "#fbbf24", // vivid yellow — warning
          500: "#f59e0b",
          600: "#d97706",
          800: "#92400e",
          900: "#78350f",
        },
        blue: {
          400: "#60a5fa", // vivid blue — info
          600: "#2563eb",
          800: "#1e40af",
          900: "#1e3a5f",
        },
        purple: {
          400: "#c084fc", // vivid purple — reports
          600: "#9333ea",
          800: "#6b21a8",
          900: "#581c87",
        },
        cyan: {
          400: "#22d3ee", // vivid cyan
          600: "#0891b2",
          800: "#155e75",
          900: "#164e63",
        },
        orange: {
          400: "#fb923c", // vivid orange
          600: "#ea580c",
          900: "#7c2d12",
        },
        indigo: {
          400: "#818cf8",
          900: "#312e81",
        },
      },
    },
  },
  plugins: [],
};
