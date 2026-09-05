/**
 * Does the console actually render, at a real viewport, without errors?
 *
 * jsdom has no layout engine and no paint, so the 256 hermetic frontend tests
 * cannot see a page that is laid out wrong — a test that queries by role passes
 * happily against a console that scrolls sideways. Every frontend defect found
 * by looking has been of that shape: a failed run labelled "Healthy", a
 * timeline rendered twice, and most recently three routes overflowing to nearly
 * twice the viewport width.
 *
 * Two checks, and both are things only a browser can answer:
 *
 *   - **No horizontal overflow.** `document.scrollWidth > clientWidth` means
 *     the page scrolls sideways, which on this console moved the sidebar off
 *     screen. The cause each time was a grid item left at its default
 *     `min-width: auto` while its content was `truncate` (`white-space:
 *     nowrap`) — so the item's min-content width is the whole unwrapped
 *     sentence, and a long health message stretched a 1,032px card to 2,511px.
 *     Every element *inside* had `min-w-0`; the grid item that needed it did
 *     not.
 *   - **No console errors.** React logs duplicate keys as an error and
 *     documents the behaviour as unsupported ("children may be duplicated
 *     and/or omitted"). The report body is keyed by line, and a report
 *     legitimately repeats one — two collectors reading nodes emit the
 *     identical `kubectl ... get nodes -o json`.
 *
 * **The vacuity guard is the point.** A blank page has no overflow and no
 * errors, and so does the sign-in gate — both are a perfect pass. Every route
 * must render a minimum amount of text and the app shell must be present, or
 * the route is reported FAILED rather than passed. That is not hypothetical:
 * the console gates on an acknowledgement in `sessionStorage`, which is fresh
 * on every headless launch, so an unseeded run screenshots the sign-in screen
 * for every route and finds nothing wrong with any of them.
 *
 * Usage — needs the backend, the console, and a headless Chrome:
 *
 *   (cd backend && AUTH_MODE=disabled ALLOW_INSECURE_NO_AUTH=true \
 *      python -m uvicorn app.main:app --port 8000 &)
 *   (cd frontend && npm run dev &)
 *   "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" \
 *      --headless=new --disable-gpu --remote-debugging-port=9222 \
 *      --user-data-dir=/tmp/console-check about:blank &
 *   node scripts/console_check.mjs
 *
 * Exit 0 clean, 1 findings, 2 the run itself could not be trusted.
 */

const BASE = process.env.CONSOLE_URL || "http://localhost:3000";
const CDP = process.env.CDP_URL || "http://127.0.0.1:9222";
const WIDTH = Number(process.env.VIEWPORT_WIDTH || 1440);
const HEIGHT = Number(process.env.VIEWPORT_HEIGHT || 1000);
const SETTLE_MS = Number(process.env.SETTLE_MS || 3000);

// What the console needs in sessionStorage to be past its sign-in gate, which
// a fresh headless profile never has. Without it every route is the sign-in
// screen — and that screen has no overflow and no console errors, so every
// check below passes while examining nothing. Set CONSOLE_TOKEN when the
// backend runs AUTH_MODE=token; the acknowledgement alone covers
// AUTH_MODE=disabled.
const SEED = { "k8s-agent-insecure-ack": "1" };
if (process.env.CONSOLE_TOKEN) SEED["k8s-agent-token"] = process.env.CONSOLE_TOKEN;

// A route needs enough text to prove it rendered its own content rather than a
// shell, a spinner or an error boundary. Tuned below the smallest real page.
const MIN_TEXT = 200;

const ROUTES = (process.env.ROUTES || "/,/investigations,/ask,/reports,/connect,/settings")
  .split(",")
  .filter(Boolean);

async function connect() {
  let pages;
  try {
    pages = await (await fetch(`${CDP}/json/list`)).json();
  } catch {
    console.error(
      `Could not reach Chrome's debugging port at ${CDP}. Launch it with ` +
        `--headless=new --remote-debugging-port=9222 (see the header of this file).`,
    );
    process.exit(2);
  }
  const page = pages.find((p) => p.type === "page");
  if (!page) {
    console.error("Chrome is running but has no page target open.");
    process.exit(2);
  }
  return new WebSocket(page.webSocketDebuggerUrl);
}

const ws = await connect();
let nextId = 0;
const pending = new Map();
let consoleErrors = [];

const send = (method, params = {}) =>
  new Promise((resolve) => {
    const id = ++nextId;
    pending.set(id, resolve);
    ws.send(JSON.stringify({ id, method, params }));
  });

ws.addEventListener("message", (event) => {
  const message = JSON.parse(event.data);
  if (message.id && pending.has(message.id)) {
    pending.get(message.id)(message.result);
    pending.delete(message.id);
    return;
  }
  if (message.method === "Runtime.consoleAPICalled" && message.params.type === "error") {
    consoleErrors.push(
      message.params.args.map((a) => a.value ?? a.description ?? a.type).join(" ").slice(0, 200),
    );
  }
  if (message.method === "Runtime.exceptionThrown") {
    const details = message.params.exceptionDetails;
    consoleErrors.push(
      `EXCEPTION: ${details.exception?.description || details.text}`.slice(0, 200),
    );
  }
});

await new Promise((resolve) => ws.addEventListener("open", resolve));
await send("Page.enable");
await send("Runtime.enable");
await send("Emulation.setDeviceMetricsOverride", {
  width: WIDTH,
  height: HEIGHT,
  deviceScaleFactor: 1,
  mobile: false,
});

// Seed on the origin, because sessionStorage is per-origin and a fresh headless
// profile has none.
await send("Page.navigate", { url: BASE });
await new Promise((r) => setTimeout(r, 900));
for (const [key, value] of Object.entries(SEED)) {
  await send("Runtime.evaluate", {
    expression: `sessionStorage.setItem(${JSON.stringify(key)}, ${JSON.stringify(value)})`,
  });
}

const results = [];
for (const route of ROUTES) {
  consoleErrors = [];
  await send("Page.navigate", { url: BASE + route });
  await new Promise((r) => setTimeout(r, SETTLE_MS));

  const probe = await send("Runtime.evaluate", {
    returnByValue: true,
    expression: `(() => {
      const doc = document.documentElement;
      const offenders = [...document.querySelectorAll("*")].filter((el) => {
        const w = el.getBoundingClientRect().width;
        const parent = el.parentElement ? el.parentElement.getBoundingClientRect().width : 0;
        return w > ${WIDTH} && parent > 0 && parent <= ${WIDTH};
      }).slice(0, 3).map((el) => ({
        tag: el.tagName,
        cls: String(el.className).slice(0, 60),
        width: Math.round(el.getBoundingClientRect().width),
        minWidth: getComputedStyle(el).minWidth,
      }));
      return {
        scrollWidth: doc.scrollWidth,
        clientWidth: doc.clientWidth,
        text: (document.body.innerText || "").trim().length,
        hasShell: Boolean(document.querySelector("nav, aside, header")),
        offenders,
      };
    })()`,
  });

  const r = probe.result.value;
  results.push({ route, ...r, errors: [...new Set(consoleErrors)] });
}

ws.close();

let findings = 0;
let untrustworthy = 0;

for (const r of results) {
  const problems = [];

  // Vacuity first: a page that rendered nothing passes every other check.
  if (r.text < MIN_TEXT || !r.hasShell) {
    untrustworthy += 1;
    console.log(
      `UNTRUSTED ${r.route}\n    rendered ${r.text} characters` +
        `${r.hasShell ? "" : " and no app shell"} — too little to have checked ` +
        `anything. A blank page and the sign-in gate both pass every assertion ` +
        `below; seed the acknowledgement and confirm the backend is up.`,
    );
    continue;
  }

  if (r.scrollWidth > r.clientWidth) {
    problems.push(
      `scrolls horizontally: ${r.scrollWidth}px of content in a ${r.clientWidth}px viewport` +
        r.offenders
          .map(
            (o) =>
              `\n      ${o.tag}.${o.cls} is ${o.width}px with min-width: ${o.minWidth}` +
              (o.minWidth === "auto"
                ? "  <- a grid/flex item at its default min-content width; it wants min-w-0"
                : ""),
          )
          .join(""),
    );
  }
  for (const error of r.errors) problems.push(`console error: ${error}`);

  if (problems.length) {
    findings += problems.length;
    console.log(`FAIL  ${r.route}`);
    for (const p of problems) console.log(`    ${p}`);
  } else {
    console.log(`ok    ${r.route}  (${r.text} chars, ${r.scrollWidth}px wide)`);
  }
}

console.log(
  `\n${results.length} route(s): ${findings} finding(s), ${untrustworthy} untrusted.`,
);
if (untrustworthy) process.exit(2);
process.exit(findings ? 1 : 0);
