/**
 * Serve a built console bundle, for harnesses that need the real thing.
 *
 * `npm run dev` is a different artefact: Vite serves unbundled modules and
 * React runs in development, where StrictMode double-renders every component.
 * A check calibrated against that is calibrated against something nobody
 * deploys — `console_journey.mjs` counts 8-10 progress rows on the bundle and
 * 15-17 on the dev server for the same investigation.
 *
 * Twenty lines rather than a dependency, for the reason there is no axios
 * here: the console's whole job is to be a static directory, and an SPA needs
 * exactly one rule beyond that — an unknown path is a client route, so it gets
 * `index.html` rather than a 404.
 */

import { createServer } from "node:http";
import { readFile } from "node:fs/promises";
import { extname, join, normalize } from "node:path";

const ROOT = process.argv[2];
const PORT = Number(process.argv[3] || 3000);

if (!ROOT) {
  console.error("usage: node scripts/serve_static.mjs <directory> [port]");
  process.exit(2);
}

const TYPES = {
  ".html": "text/html; charset=utf-8",
  ".js": "text/javascript; charset=utf-8",
  ".css": "text/css; charset=utf-8",
  ".json": "application/json",
  ".svg": "image/svg+xml",
  ".ico": "image/x-icon",
  ".png": "image/png",
  ".woff2": "font/woff2",
};

createServer(async (request, response) => {
  const path = decodeURIComponent(
    new URL(request.url, "http://localhost").pathname,
  );
  // `normalize` before joining, so a request for `/../../etc/passwd` cannot
  // leave the directory being served.
  const asked = join(ROOT, normalize(path).replace(/^(\.\.[/\\])+/, ""));

  for (const candidate of [asked, join(ROOT, "index.html")]) {
    try {
      const body = await readFile(candidate);
      response.writeHead(200, {
        "Content-Type": TYPES[extname(candidate)] || "application/octet-stream",
        "Cache-Control": "no-store",
      });
      response.end(body);
      return;
    } catch {
      // Fall through to index.html: an unknown path is a client-side route.
    }
  }

  response.writeHead(404).end("not found");
}).listen(PORT, () =>
  console.log(`serving ${ROOT} on http://localhost:${PORT}`),
);
