/** @type {import('tailwindcss').Config} */
export default {
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        gray: {
          950: "#0d1117", // page background (GitHub dark)
          900: "#161b22", // card/panel background
          850: "#1c2129", // sidebar/header background
          800: "#30363d", // borders
          700: "#484f58", // hover states, dividers
          600: "#6e7681", // muted UI elements
          500: "#8b949e", // tertiary text
          400: "#b1bac4", // secondary text
          300: "#c9d1d9", // primary text
          200: "#e6edf3", // headings
          100: "#f0f6fc", // bright text
        },
        emerald: {
          400: "#3fb950", // vibrant green (GitHub green)
          500: "#2ea043",
          600: "#238636",
          700: "#196c2e",
          800: "#0f5323",
          900: "#033a16",
        },
        red: {
          400: "#f85149", // vivid coral (GitHub red)
          500: "#da3633",
          600: "#b62324",
          800: "#8e1519",
          900: "#67060c",
        },
        amber: {
          400: "#d29922", // golden amber (GitHub yellow)
          500: "#bb8009",
          600: "#9e6a03",
          800: "#7a4f01",
          900: "#5c3d02",
        },
        blue: {
          400: "#58a6ff", // vibrant blue (GitHub blue - primary accent)
          500: "#388bfd",
          600: "#1f6feb",
          800: "#0d419d",
          900: "#0c2d6b",
        },
        purple: {
          400: "#bc8cff", // vivid violet (GitHub purple)
          500: "#a371f7",
          600: "#8957e5",
          800: "#6639ba",
          900: "#3c1e70",
        },
        cyan: {
          400: "#39d2c0", // bright teal
          500: "#2bb5a4",
          600: "#1b9e8f",
          800: "#0f6d64",
          900: "#083d39",
        },
        orange: {
          400: "#f0883e", // bright orange
          500: "#db6d28",
          600: "#bd561d",
          900: "#6e3208",
        },
        indigo: {
          400: "#79c0ff", // light sky blue
          900: "#0a3069",
        },
        pink: {
          400: "#f778ba", // vibrant pink
          900: "#5e103e",
        },
      },
    },
  },
  plugins: [],
};
