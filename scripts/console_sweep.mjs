/**
 * Every console route, every control on it, and the text each one leaves on
 * screen — for a person to read against the data.
 *
 * `console_check.mjs` asks whether a page renders; `console_journey.mjs` and
 * `agent_journey.mjs` drive the flows that matter most. This is the wide net
 * underneath them: load each route, click each link, button, tab and summary
 * from a fresh load of that route, and record what happened.
 *
 * **What it records is not what it found.** Its first full run — 17 routes,
 * 220 clicks against a real cluster and a real agent — logged zero non-2xx
 * responses and zero console errors. The eleven defects that sweep produced
 * (a Secret that existed reported as the root cause, remediation commands for
 * an unrelated workload, "Model-authored" on reports no model touched, a trend
 * labelled "happening less often" for a finding present in every run) were all
 * in the *text*, and were found by reading each page's dump beside the
 * investigation JSON it was rendered from. So every state is written to
 * `OUT/text/*.txt` with its URL, and a screenshot beside it. The automated
 * findings are the floor; the reading is the sweep.
 *
 * Lists of like controls — sixty evidence rows, a citation chip per signal —
 * are capped at three clicks each, keyed by the list they sit in. Reloading
 * the route before every one of sixty identical rows took over an hour and
 * found nothing the first three did not.
 *
 * Usage (see CLAUDE.md, *Sweeping the console*):
 *
 *   CONSOLE_URL=http://localhost:3000 API_URL=http://localhost:8000 \
 *   CONSOLE_TOKEN=<token> OUT=~/sweep node scripts/console_sweep.mjs
 *
 * `ROUTES` and `IDS` (comma lists) narrow it. Exit 0 with no automated
 * findings, 1 with some, 2 when the run could not be trusted: a route that
 * rendered the sign-in gate or next to nothing checks nothing.
 */

import { appendFileSync, mkdirSync, writeFileSync } from "node:fs";
import { join } from "node:path";

const BASE = process.env.CONSOLE_URL || "http://localhost:3000";
const API = process.env.API_URL || "http://localhost:8000";
const CDP = process.env.CDP_URL || "http://127.0.0.1:9222";
const TOKEN = process.env.CONSOLE_TOKEN || "";
const OUT = process.env.OUT || "console-sweep";
const PER_LIST = Number(process.env.PER_LIST || 3);
const SETTLE_MS = Number(process.env.SETTLE_MS || 2200);
const MIN_TEXT = 150;

for (const dir of ["text", "shots"]) mkdirSync(join(OUT, dir), { recursive: true });

const wait = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
let where = "start";
const counts = {};
const record = (kind, detail = {}) => {
  counts[kind] = (counts[kind] || 0) + 1;
  const entry = { where, kind, ...detail };
  appendFileSync(join(OUT, "log.jsonl"), `${JSON.stringify(entry)}\n`);
  if (!["click", "inventory"].includes(kind)) {
    console.log(`  [${kind}] ${where} ${JSON.stringify(detail).slice(0, 300)}`);
  }
};

/* ------------------------------------------------------------------ CDP -- */

const target = await (await fetch(`${CDP}/json/new?about:blank`, { method: "PUT" })).json();
const ws = new WebSocket(target.webSocketDebuggerUrl);
let nextId = 0;
const pending = new Map();
const requests = new Map();
const send = (method, params = {}) =>
  new Promise((resolve) => {
    const id = ++nextId;
    pending.set(id, resolve);
    ws.send(JSON.stringify({ id, method, params }));
  });

ws.addEventListener("message", (event) => {
  const message = JSON.parse(event.data);
  if (message.id && pending.has(message.id)) {
    pending.get(message.id)(message.result ?? { error: message.error });
    pending.delete(message.id);
    return;
  }
  const p = message.params;
  switch (message.method) {
    case "Network.requestWillBeSent":
      requests.set(p.requestId, { method: p.request.method, url: p.request.url });
      break;
    case "Network.responseReceived":
      if (p.response.status >= 400) {
        const request = requests.get(p.requestId) || {};
        record("http", { status: p.response.status, method: request.method, url: p.response.url });
      }
      break;
    case "Network.loadingFailed": {
      const request = requests.get(p.requestId) || {};
      // A navigation cancels the previous page's in-flight GETs; that is the
      // reload between clicks, not a failure.
      if (!(/ERR_ABORTED/.test(p.errorText) && request.method === "GET")) {
        record("netfail", { error: p.errorText, method: request.method, url: request.url });
      }
      break;
    }
    case "Runtime.consoleAPICalled":
      if (p.type === "error" || p.type === "warning") {
        record("console", {
          type: p.type,
          text: p.args.map((a) => a.value ?? a.description ?? a.type).join(" ").slice(0, 400),
        });
      }
      break;
    case "Runtime.exceptionThrown":
      record("exception", {
        text: (p.exceptionDetails.exception?.description || p.exceptionDetails.text).slice(0, 400),
      });
      break;
    case "Page.javascriptDialogOpening":
      record("dialog", { type: p.type, message: p.message });
      send("Page.handleJavaScriptDialog", { accept: false });
      break;
  }
});
await new Promise((resolve) => ws.addEventListener("open", resolve));
const evaluate = async (expression) =>
  (await send("Runtime.evaluate", { expression, awaitPromise: true, returnByValue: true }))?.result
    ?.value;

await send("Page.enable");
await send("Runtime.enable");
await send("Network.enable");
await send("Emulation.setDeviceMetricsOverride", {
  width: 1440,
  height: 1000,
  deviceScaleFactor: 1,
  mobile: false,
});

const api = async (path) =>
  (await fetch(`${API}${path}`, { headers: { Authorization: `Bearer ${TOKEN}` } })).json();

let shot = 0;
async function snapshot(label) {
  const name = `${String(++shot).padStart(3, "0")}_${label.replace(/[^a-z0-9]+/gi, "_").slice(0, 80)}`;
  const text = (await evaluate("document.body.innerText")) || "";
  writeFileSync(join(OUT, "text", `${name}.txt`), `URL ${await evaluate("location.href")}\n\n${text}`);
  const png = await send("Page.captureScreenshot", { format: "png" });
  if (png.data) writeFileSync(join(OUT, "shots", `${name}.png`), Buffer.from(png.data, "base64"));
  const overflow = await evaluate(
    "document.documentElement.scrollWidth - document.documentElement.clientWidth",
  );
  if (overflow > 0) record("overflow", { px: overflow });
  return text;
}

async function go(path) {
  where = path;
  await send("Page.navigate", { url: BASE + path });
  await wait(SETTLE_MS);
}

const CONTROLS = `[...document.querySelectorAll("a[href], button, summary, [role=button], [role=tab]")]
  .filter((el) => el.offsetParent !== null || el.tagName === "SUMMARY")`;
const describe = `(el) => ({
  tag: el.tagName,
  text: (el.innerText || el.getAttribute("aria-label") || "").trim().slice(0, 60),
  href: el.getAttribute("href"),
  disabled: el.disabled === true,
  list: (() => {
    const row = el.closest("li, tr");
    if (!row || !row.parentElement) return "";
    const list = row.parentElement;
    return list.tagName + [...list.parentElement.children].indexOf(list) + ":" +
      String(list.parentElement.className).slice(0, 30) + ":" + el.tagName;
  })(),
})`;

let untrusted = 0;
async function crawl(route) {
  await go(route);
  const text = await snapshot(`route ${route}`);
  if (text.length < MIN_TEXT || /Paste the token issued to you/.test(text)) {
    untrusted += 1;
    record("untrusted", { chars: text.length });
    return;
  }
  const controls = await evaluate(`${CONTROLS}.map(${describe})`);
  record("inventory", { route, count: controls.length });

  const seen = new Set();
  const perList = new Map();
  for (const control of controls) {
    const key = `${control.tag}|${control.text}|${control.href}`;
    if (seen.has(key) || control.disabled || control.text === "Sign out") continue;
    seen.add(key);
    if (control.list) {
      const n = (perList.get(control.list) || 0) + 1;
      perList.set(control.list, n);
      if (n > PER_LIST) continue;
    }
    if (control.href && /^https?:/.test(control.href) && !control.href.startsWith(BASE)) {
      record("external-link", { text: control.text, href: control.href });
      continue;
    }

    await go(route);
    where = `${route} :: ${control.tag} "${control.text}"${control.href ? ` -> ${control.href}` : ""}`;
    const before = await evaluate("location.href");
    const clicked = await evaluate(`(() => {
      const el = ${CONTROLS}.find((el) => {
        const d = (${describe})(el);
        return d.tag === ${JSON.stringify(control.tag)} && d.text === ${JSON.stringify(control.text)} &&
          d.href === ${JSON.stringify(control.href)};
      });
      if (!el) return false;
      el.click();
      return true;
    })()`);
    if (!clicked) {
      record("vanished", { control });
      continue;
    }
    await wait(SETTLE_MS);
    const after = await evaluate("location.href");
    const shown = await snapshot(`click ${route} ${control.text || control.href}`);
    record("click", { navigated: before !== after ? after : null, chars: shown.length });
    if (shown.length < MIN_TEXT) record("blank-after-click", { text: shown.slice(0, 200) });
  }
}

try {
  await go("/");
  await evaluate(`sessionStorage.setItem("k8s-agent-token", ${JSON.stringify(TOKEN)})`);

  const clusters = (await api("/clusters")).items.map((item) => item.name);
  const history = (await api("/investigations")).items || [];
  // The newest investigation of each cluster, unless named.
  const ids = process.env.IDS
    ? process.env.IDS.split(",")
    : [...new Map([...history].reverse().map((item) => [item.context, item.id])).values()];

  const routes = process.env.ROUTES
    ? process.env.ROUTES.split(",")
    : [
        "/",
        "/investigations",
        "/ask",
        "/reports",
        "/connect",
        "/settings",
        ...clusters.flatMap((cluster) =>
          ["", "?tab=investigations", "?tab=evidence", "?tab=events", "?tab=reports"].map(
            (tab) => `/clusters/${encodeURIComponent(cluster)}${tab}`,
          ),
        ),
        ...ids.map((id) => `/investigations/${id}`),
        "/no-such-page",
      ];
  for (const route of routes) await crawl(route);
} catch (error) {
  untrusted += 1;
  record("sweep-crash", { error: String(error.stack || error).slice(0, 600) });
} finally {
  await send("Target.closeTarget", { targetId: target.id });
  ws.close();
}

const automated = ["http", "netfail", "console", "exception", "dialog", "overflow", "vanished", "blank-after-click"]
  .reduce((sum, kind) => sum + (counts[kind] || 0), 0);
console.log(`\nsweep: ${JSON.stringify(counts)}`);
console.log(`text for reading: ${join(OUT, "text")} (${shot} states)`);
process.exit(untrusted ? 2 : automated ? 1 : 0);
