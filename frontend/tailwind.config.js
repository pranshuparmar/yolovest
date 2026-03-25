/** @type {import('tailwindcss').Config} */
export default {
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        // Monokai Pro Dimmed gray scale
        gray: {
          950: "#16171e", // page background (deepest)
          900: "#1d1e26", // card/panel background
          800: "#2a2c37", // borders, elevated surfaces
          700: "#353847", // subtle hover, dividers
          600: "#4a4e5e", // muted UI elements
          500: "#6e7288", // comments, tertiary text
          400: "#908e96", // secondary text
          300: "#b0aea8", // stronger secondary text
          200: "#c3c0bb", // primary text
          100: "#d5d3cd", // headings, bright text
        },
        // Monokai Pro Dimmed accent colors
        emerald: {
          400: "#a4cc78", // green (strings) — BUY, success
          500: "#8fb865",
          600: "#7da352",
          700: "#5c7a3c",
          800: "#3a4f28",
          900: "#2a3a1e",
        },
        red: {
          400: "#f38e82", // red (keywords) — SELL, error
          500: "#e06050",
          600: "#c44840",
          800: "#6b2a24",
          900: "#4a1e1a",
        },
        amber: {
          400: "#f9cc6c", // yellow (classes) — warning
          500: "#e0b550",
          600: "#c8a035",
          800: "#5e4c1e",
          900: "#3e3216",
        },
        blue: {
          400: "#78cfe2", // cyan/blue (functions) — info
          600: "#4a9db3",
          800: "#28505e",
          900: "#1c3840",
        },
        purple: {
          400: "#c7a4e0", // purple (constants) — reports
          600: "#9470ad",
          800: "#4a3758",
          900: "#352842",
        },
        cyan: {
          400: "#78cfe2", // same as blue for Monokai coherence
          600: "#4a9db3",
          800: "#28505e",
          900: "#1c3840",
        },
        orange: {
          400: "#f9967b", // orange (operators)
          600: "#c46a50",
          900: "#4a2a1e",
        },
      },
    },
  },
  plugins: [],
};
