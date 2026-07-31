/**
 * Design tokens from docs/CONSOLE_REDESIGN.md §8–§10.
 *
 * These replace the five unnamed dark hexes and the eight decorative hues that
 * `App.tsx` currently spells out as literals. Semantic colour is the only
 * colour that carries meaning, so nothing here is named for a hue.
 *
 * Nothing consumes these yet — Phase 0 adds the vocabulary without changing a
 * pixel. Later phases migrate components onto it.
 *
 * @type {import('tailwindcss').Config}
 */
export default {
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        // Surfaces, in stacking order.
        canvas: "#0A0C10",
        surface: "#12151B",
        raised: "#171B23",
        overlay: "#1D222C",
        line: { DEFAULT: "#232935", muted: "#1A1F28" },
        // Text, by descending emphasis.
        ink: { DEFAULT: "#E6E9EE", 2: "#A2AAB7", 3: "#6E7681" },
        // Severity and provenance. `ai` marks model-authored prose — a
        // provenance signal, not a premium one (§8.3).
        critical: "#F4645F",
        warning: "#E0A23C",
        healthy: "#4FB477",
        info: "#5B9DF9",
        ai: "#A78BFA",
      },
      fontFamily: {
        sans: ["Inter var", "ui-sans-serif", "system-ui", "sans-serif"],
        mono: ["JetBrains Mono", "ui-monospace", "SFMono-Regular", "monospace"],
      },
      fontSize: {
        // Six steps, floor of 13px. There is no smaller size (§9.2).
        label: ["0.75rem", { lineHeight: "1rem", fontWeight: "600", letterSpacing: "0.06em" }],
        sm: ["0.8125rem", { lineHeight: "1.25rem" }],
        body: ["0.9375rem", { lineHeight: "1.5rem" }],
        h2: ["1.0625rem", { lineHeight: "1.5rem", fontWeight: "600" }],
        h1: ["1.375rem", { lineHeight: "1.75rem", fontWeight: "600" }],
        display: ["1.75rem", { lineHeight: "2.125rem", fontWeight: "600" }],
      },
      maxWidth: {
        // An investigation is read, not scanned (§10).
        document: "1080px",
        measure: "68ch",
      },
      transitionDuration: {
        // Ceiling of 200ms; anything slower reads as latency under pressure.
        fast: "120ms",
        base: "160ms",
        slow: "200ms",
      },
    },
  },
  plugins: [],
};
