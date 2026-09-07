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
 * **A rendered page is not the same as a page that could have overflowed**,
 * and that is a second, quieter vacuity this could not report. `min-w-0` was
 * reverted once and the run came back clean, because a fresh successful
 * investigation had replaced the long health message and there was nothing
 * left to overflow — the mutation did not reproduce, which means the check was
 * inert for that scenario, not that it worked. So each run now reports the
 * widest run of text that cannot wrap: a grid item at `min-width: auto` is
 * only forced past its track by content whose min-content width exceeds it, so
 * if the widest such run is narrower than the viewport, no single item could
 * have scrolled the page. Measured across the full 2×2 — with the defect
 * present and a 241-character root cause the page scrolls to 2,010px; with the
 * defect present and an 89-character one it passes clean at 618px and says
 * NO TRIGGER. Reported and not enforced: a console with no long content is a
 * legitimate state, and failing on it is the over-strict direction.
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
      // The widest run of text that cannot wrap. A grid item left at
      // min-width auto is only forced past its track by content whose
      // min-content width exceeds it, and for a truncated element that is the
      // whole unwrapped sentence -- scrollWidth measures it even while it
      // renders elided. If the widest one on a route is narrower than the
      // viewport, no single item could have scrolled the page, and this
      // route's overflow check had nothing to detect.
      let widestNowrap = 0;
      let widestNowrapTag = "";
      for (const el of document.querySelectorAll("*")) {
        if (getComputedStyle(el).whiteSpace !== "nowrap") continue;
        if (el.scrollWidth <= widestNowrap) continue;
        widestNowrap = el.scrollWidth;
        widestNowrapTag = el.tagName + "." + String(el.className).slice(0, 40);
      }

      return {
        scrollWidth: doc.scrollWidth,
        clientWidth: doc.clientWidth,
        text: (document.body.innerText || "").trim().length,
        hasShell: Boolean(document.querySelector("nav, aside, header")),
        offenders,
        widestNowrap,
        widestNowrapTag,
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

// **Whether the overflow check had anything to detect**, which is a different
// question from whether it passed and one this script could not previously
// answer. `min-w-0` was reverted once and the run came back clean, because a
// fresh successful investigation had replaced the long health message and
// there was nothing left to overflow — a mutation that does not reproduce
// means the check is inert for that scenario, not that it works.
//
// Measured against exactly that: an 89-character root cause put the widest
// unwrapped run at ~600px and the mutation was invisible; a 241-character one
// put it at 1,694px and the mutation scrolled the page to 2,010px. So the
// criterion is whether any route carries a nowrap run wider than the viewport.
// It is reported rather than enforced — a console with no long content is a
// legitimate state, and failing on it would be the over-strict direction.
const widest = results.reduce(
  (best, r) => (r.widestNowrap > (best?.widestNowrap ?? 0) ? r : best),
  null,
);
if (widest && widest.widestNowrap > widest.clientWidth) {
  console.log(
    `Overflow check had teeth: ${widest.route} carries an unwrapped run of ` +
      `${widest.widestNowrap}px in a ${widest.clientWidth}px viewport ` +
      `(${widest.widestNowrapTag}).`,
  );
} else {
  console.log(
    `NO TRIGGER: the widest unwrapped text anywhere was ` +
      `${widest ? widest.widestNowrap : 0}px, inside a ${WIDTH}px viewport, so no ` +
      `single item could have scrolled the page. The overflow check passed ` +
      `without being able to fail — point the console at a cluster whose last ` +
      `investigation produced a long root cause before believing it.`,
  );
}

if (untrustworthy) process.exit(2);
process.exit(findings ? 1 : 0);
