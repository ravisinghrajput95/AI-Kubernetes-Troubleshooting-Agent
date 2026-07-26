import { defineConfig } from "vitest/config";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  envPrefix: ["VITE_", "react_PUBLIC_"],
  server: {
    host: "0.0.0.0",
    port: 3000,
  },
  build: {
    rollupOptions: {
      output: {
        // Split rarely-changing vendor code from application code. Total bytes
        // are unchanged; the benefit is on repeat visits, where only the small
        // app chunk is re-downloaded after a deploy. Splitting the app's own
        // panels would not pay for itself — they are a small fraction of the
        // bundle and each extra chunk costs a round trip.
        manualChunks(id: string) {
          if (!id.includes("node_modules")) {
            return undefined;
          }
          // Match on the package directory so subpath imports such as
          // `react-dom/client` land in the same chunk as their package.
          const pkg = id.split("node_modules/")[1]?.split("/")[0];
          if (pkg === "react" || pkg === "react-dom" || pkg === "scheduler") {
            return "react";
          }
          if (pkg === "@tanstack") {
            return "query";
          }
          return undefined;
        },
      },
    },
  },
  test: {
    environment: "jsdom",
    globals: true,
    include: ["src/**/*.test.{ts,tsx}"],
    setupFiles: ["src/test/setup.ts"],
  },
});

