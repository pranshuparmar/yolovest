/** @type {import('tailwindcss').Config} */
export default {
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        gray: {
          950: "#0a0a0a", // page background (near-black)
          900: "#171717", // card/panel background (visible lift)
          850: "#1f1f1f", // sidebar/header background (distinct from both)
          800: "#2e2e2e", // borders (clearly visible)
          700: "#404040", // hover states, dividers
          600: "#525252", // muted UI elements
          500: "#737373", // tertiary text
          400: "#a3a3a3", // secondary text
          300: "#d4d4d4", // primary text
          200: "#e5e5e5", // headings
          100: "#f5f5f5", // bright text
        },
        emerald: {
          400: "#34d399",
          500: "#10b981",
          600: "#059669",
          700: "#047857",
          800: "#065f46",
          900: "#064e3b",
        },
        red: {
          400: "#f87171",
          500: "#ef4444",
          600: "#dc2626",
          800: "#991b1b",
          900: "#7f1d1d",
        },
        amber: {
          400: "#fbbf24",
          500: "#f59e0b",
          600: "#d97706",
          800: "#92400e",
          900: "#78350f",
        },
        blue: {
          400: "#60a5fa",
          600: "#2563eb",
          800: "#1e40af",
          900: "#1e3a5f",
        },
        purple: {
          400: "#c084fc",
          600: "#9333ea",
          800: "#6b21a8",
          900: "#581c87",
        },
        cyan: {
          400: "#22d3ee",
          600: "#0891b2",
          800: "#155e75",
          900: "#164e63",
        },
        orange: {
          400: "#fb923c",
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
