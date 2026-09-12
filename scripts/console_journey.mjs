/**
 * Does the console *work*, driven the way a person drives it?
 *
 * `console_check.mjs` loads each route and asks whether it rendered without
 * overflowing or logging an error. That is a real question and it has caught
 * real defects, but it never signs in, never starts an investigation and never
 * downloads anything — so the console could be, and was, comprehensively
 * broken while every route passed.
 *
 * **Two defects shipped in exactly that gap, and they were the two most
 * visible things this product does.** `EventSource` cannot send an
 * `Authorization` header and `<a href>` cannot either, so in every deployment
 * with authentication configured — which, since `AUTH_MODE` lost its default,
 * is all of them — the live progress stream was answered 401 and silently fell
 * back to polling (F29), and all three report downloads were answered 401 and
 * saved the JSON error body under the name of a report (F30). Neither failed
 * loudly. 1,600 backend tests, 276 frontend tests, 45 mutation pairs and a
 * required kind-based integration job were all green throughout.
 *
 * Nothing below is a new kind of cleverness. It is the product, used:
 *
 *   sign in -> start an investigation -> watch it stream -> download the PDF
 *
 * **What it asserts that a unit test cannot.** Every assertion here is about a
 * request the *browser* chose to make, or a byte the browser actually received:
 *
 *   - the stream request happened **and was answered 200** (F29 made it 401)
 *   - progress arrived over that stream rather than the polling fallback,
 *     judged by counting the fallback's own requests
 *   - the timeline on screen holds events the backend emitted
 *   - the report request was answered 200 (F30 made it 401)
 *   - the file on disk **begins with `%PDF`** (F30 saved `{"detail": ...}`)
 *
 * The hook's own tests could not see F29 because their double called
 * `onmessage` directly: no request was ever made, so no header could be
 * missing. The required CI SSE check could not see it either, because it
 * streams with an `Authorization` header — a header no browser can send.
 * Proving the server streams says nothing about whether the client reaches it.
 *
 * **The vacuity guards are the load-bearing half**, as everywhere else here. A
 * journey that never got past the sign-in gate makes every assertion below
 * vacuously true, and so does one against a cluster that answered nothing. The
 * sharpest of them is on the stream itself: "the console did not poll" is
 * satisfied perfectly by a console that did nothing at all, so a run that saw
 * no stream request is REFUSED rather than passed.
 *
 * Usage — needs the backend, the console, a cluster worth investigating, and a
 * headless Chrome:
 *
 *   "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" \
 *      --headless=new --disable-gpu --remote-debugging-port=9222 \
 *      --user-data-dir=/tmp/journey about:blank &
 *   CONSOLE_TOKEN=... API_URL=http://127.0.0.1:8000 node scripts/console_journey.mjs
 *
 * Exit 0 clean, 1 findings, 2 the run itself could not be trusted.
 */

import { mkdtempSync, readdirSync, readFileSync, statSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

const BASE = process.env.CONSOLE_URL || "http://localhost:3000";
const API = process.env.API_URL || "http://127.0.0.1:8000";
const CDP = process.env.CDP_URL || "http://127.0.0.1:9222";
const TOKEN = process.env.CONSOLE_TOKEN || "";
const CLUSTER = process.env.CONSOLE_CLUSTER || "";
const WIDTH = Number(process.env.VIEWPORT_WIDTH || 1440);
const HEIGHT = Number(process.env.VIEWPORT_HEIGHT || 1000);
// An investigation of a real namespace is seconds; a slow CI runner and a cold
// cache is not. Generous, because a timeout here is reported as untrusted
// rather than as a defect and a flaky required job is worse than a slow one.
const RUN_TIMEOUT_MS = Number(process.env.JOURNEY_TIMEOUT_MS || 180000);

// A healthy console polls **zero** times: the fallback only starts when the
// stream fails or is absent, so any polling at all is the degraded path. One
// is allowed as margin for a stray re-render, not because one is expected.
//
// Calibrated rather than guessed, and the first guess was wrong: 3 was chosen
// on the reasoning that a 20-second investigation would poll a dozen times,
// but the F18 collection cache makes a repeat run finish in two or three
// seconds and the F29 mutant carried the *whole* investigation in **2** polls
// — under the threshold, so the check sat there looking right and never fired.
// Measured: two clean runs at 0 and 0, the mutant at 2.
const MAX_TOLERATED_POLLS = 1;
// Below this the timeline is a shell rather than a run: queued and started
// alone would pass "some events arrived" while every progress frame was lost.
//
// Measured on the built bundle against a 49-pod cluster — 8 to 10 across four
// runs, against **4** with the stream refused and the polling fallback
// carrying it. Six is the midpoint of that gap. The dev server gives 15-17 because React's StrictMode
// double-renders, so calibrate on the bundle: it is what ships and what CI
// serves.
const MIN_PROGRESS_EVENTS = 6;
// A report smaller than this is an error body, not a document.
const MIN_REPORT_BYTES = 2000;

const downloadDir = mkdtempSync(join(tmpdir(), "k8s-journey-"));

/* ------------------------------------------------------------------ CDP -- */

async function connect() {
  let target;
  try {
    target = await (
      await fetch(`${CDP}/json/new?about:blank`, { method: "PUT" })
    ).json();
  } catch {
    console.error(
      `Could not reach Chrome's debugging port at ${CDP}. Launch it with ` +
        `--headless=new --remote-debugging-port=9222 (see the header of this file).`,
    );
    process.exit(2);
  }
  return new WebSocket(target.webSocketDebuggerUrl);
}

const ws = await connect();
let nextId = 0;
const pending = new Map();

/** Every response the page received, so assertions read the wire, not the DOM. */
const responses = [];
const consoleErrors = [];
const downloads = [];

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
  const { method, params } = message;
  if (method === "Network.responseReceived") {
    responses.push({
      url: params.response.url,
      status: params.response.status,
    });
  }
  if (method === "Runtime.consoleAPICalled" && params.type === "error") {
    consoleErrors.push(
      params.args
        .map((a) => a.value ?? a.description ?? a.type)
        .join(" ")
        .slice(0, 200),
    );
  }
  if (method === "Browser.downloadProgress" && params.state === "completed") {
    downloads.push(params.guid);
  }
});

await new Promise((resolve) => ws.addEventListener("open", resolve));
await send("Page.enable");
await send("Runtime.enable");
await send("Network.enable");
await send("Browser.setDownloadBehavior", {
  behavior: "allow",
  downloadPath: downloadDir,
  eventsEnabled: true,
});
await send("Emulation.setDeviceMetricsOverride", {
  width: WIDTH,
  height: HEIGHT,
  deviceScaleFactor: 1,
  mobile: false,
});

const evaluate = async (expression) =>
  (
    await send("Runtime.evaluate", {
      expression,
      awaitPromise: true,
      returnByValue: true,
    })
  ).result?.value;

const settle = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

/** Poll a page predicate until it holds, rather than sleeping a guessed span. */
async function until(expression, timeoutMs, everyMs = 500) {
  const deadline = Date.now() + timeoutMs;
  for (;;) {
    if (await evaluate(expression)) return true;
    if (Date.now() > deadline) return false;
    await settle(everyMs);
  }
}

/** How many progress rows the live timeline is showing, right now. */
const PROGRESS_ROWS = `[...document.querySelectorAll("li, tr")]
  .map(el => el.innerText || "")
  .filter(t => /Collecting evidence|Collected |Analyzing|Root Cause Generated|Report generated|Investigation (started|queued|complete)/i.test(t))
  .length`;

const seen = (pattern) => responses.filter((r) => pattern.test(r.url));

/* -------------------------------------------------------------- journey -- */

const findings = [];
const refusals = [];
const note = (line) => console.log(`  ${line}`);

console.log(`\nDriving ${BASE} against ${API}\n`);

// --- 1. Sign in, through the form a person is given ------------------------
//
// Seeding `sessionStorage` would be shorter and is what `console_check.mjs`
// does, because it only needs to be *past* the gate. Here the gate is the
// first step of the journey and nothing else in a browser covers it.
await send("Page.navigate", { url: BASE });
await settle(1500);

const gate = await evaluate(`(() => {
  const input = document.querySelector('input[type="password"], input[type="text"]');
  const button = [...document.querySelectorAll("button")].find(b => /sign in/i.test(b.textContent));
  return JSON.stringify({ hasInput: Boolean(input), hasButton: Boolean(button) });
})()`);
const gateState = JSON.parse(gate || "{}");

if (TOKEN && gateState.hasInput && gateState.hasButton) {
  // React tracks the input's value internally, so assigning `.value` and
  // dispatching `input` is the only way a script can type into a controlled
  // component and have the component agree it happened.
  await evaluate(`(() => {
    const input = document.querySelector('input[type="password"], input[type="text"]');
    const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, "value").set;
    setter.call(input, ${JSON.stringify(TOKEN)});
    input.dispatchEvent(new Event("input", { bubbles: true }));
    [...document.querySelectorAll("button")].find(b => /sign in/i.test(b.textContent)).click();
    return true;
  })()`);
  await settle(2500);
} else if (!TOKEN) {
  // AUTH_MODE=disabled still shows an acknowledgement gate.
  await evaluate(`sessionStorage.setItem("k8s-agent-insecure-ack", "1")`);
  await send("Page.navigate", { url: BASE });
  await settle(1500);
}

const signedIn = await evaluate(`(() => {
  const shell = Boolean(document.querySelector("nav, aside, header"));
  const stillGated = Boolean(document.querySelector('input[type="password"]'));
  return shell && !stillGated;
})()`);

if (!signedIn) {
  refusals.push(
    "the sign-in gate never cleared, so nothing below was driven. Every " +
      "assertion in this journey is vacuously true against the sign-in screen — " +
      "which is exactly what a fresh headless profile shows. Check CONSOLE_TOKEN.",
  );
} else {
  note("signed in through the form");
}

// --- 2. Start an investigation, by clicking the button --------------------
let investigationId = "";
if (signedIn) {
  await send("Page.navigate", { url: `${BASE}/investigations` });
  await settle(2500);

  if (CLUSTER) {
    await evaluate(`(() => {
      for (const select of document.querySelectorAll("select")) {
        const option = [...select.options].find(o => o.value === ${JSON.stringify(CLUSTER)});
        if (!option) continue;
        const setter = Object.getOwnPropertyDescriptor(window.HTMLSelectElement.prototype, "value").set;
        setter.call(select, option.value);
        select.dispatchEvent(new Event("change", { bubbles: true }));
        return true;
      }
      return false;
    })()`);
    await settle(400);
  }

  const clicked = await evaluate(`(() => {
    const button = [...document.querySelectorAll("button")]
      .find(b => /start investigation/i.test(b.textContent) && !b.disabled);
    if (!button) return false;
    button.click();
    return true;
  })()`);

  if (!clicked) {
    refusals.push(
      "no enabled 'Start investigation' button was found on /investigations, " +
        "so no investigation was started and the stream and report assertions " +
        "below had nothing to describe.",
    );
  } else {
    // The id is on the wire: the stream request names it. Waiting for it is
    // also what proves the console *tried* to stream at all, which is the
    // vacuity guard the polling assertion rests on.
    const deadline = Date.now() + 30000;
    while (Date.now() < deadline && !investigationId) {
      const match = responses.find((r) =>
        /\/investigations\/[0-9a-f-]{16,}\/events/.test(r.url),
      );
      if (match)
        investigationId = match.url.match(
          /investigations\/([0-9a-f-]{16,})\/events/,
        )[1];
      else await settle(400);
    }
    note(clicked ? "started an investigation from the console" : "");
  }
}

// --- 3. Did progress arrive over the stream, or the fallback? -------------
//
// This is F29, and the assertion has to be two-sided. "The console did not
// poll" is satisfied perfectly by a console that did nothing, so the run is
// refused unless a stream request was actually made.
// **Live views, not snapshots.** These were `const` arrays computed here and
// read again after the run, so the polling count was taken before any polling
// could have happened and that assertion could never fire — found by watching
// the F29 mutation report `poll 0 req` while the fallback was plainly carrying
// the whole investigation. A check that cannot fail is the defect this harness
// exists to catch, committed in the harness itself.
const streamRequests = () => seen(/\/investigations\/[^/]+\/events/);
const pollRequests = () => seen(/\/investigations\/[^/]+\/status/);

if (signedIn && !streamRequests().length) {
  refusals.push(
    "the console never requested the event stream at all, so 'progress did " +
      "not arrive by polling' proves nothing. Nothing was measured about the " +
      "transport.",
  );
} else if (streamRequests().length) {
  const refused = streamRequests().filter((r) => r.status !== 200);
  if (refused.length) {
    findings.push(
      `the event stream was refused: ${refused.map((r) => r.status).join(", ")}. ` +
        `An EventSource cannot send an Authorization header, so this is 401 in ` +
        `every authenticated deployment and the console silently polls instead ` +
        `(F29). The stream must be read with fetch.`,
    );
  } else {
    note(`event stream opened and answered ${streamRequests()[0].status}`);
  }
}

// --- 4. Let it finish, and read what the console shows --------------------
let timeline = { progress: 0, terminal: false, text: 0 };
if (investigationId) {
  // **Sampled while it runs, not after.** The live timeline is replaced by the
  // result view the moment the investigation finishes, so counting rows at the
  // end measures the report page — which renders a handful of rows whatever
  // the transport did. The peak is what says progress was delivered *as it
  // happened*, which is the whole claim the stream exists to support.
  let peakRows = 0;
  const deadline = Date.now() + RUN_TIMEOUT_MS;
  for (;;) {
    peakRows = Math.max(peakRows, Number(await evaluate(PROGRESS_ROWS)) || 0);
    const done = await evaluate(
      `/Root cause|Investigation complete|No cluster read succeeded/i.test(document.body.innerText)`,
    );
    if (done || Date.now() > deadline) break;
    await settle(400);
  }
  await settle(1200);

  timeline = JSON.parse(
    (await evaluate(`(() => {
      const text = document.body.innerText;
      return JSON.stringify({
        terminal: /Root cause|Investigation complete|succeeded|failed/i.test(text),
        text: text.trim().length,
        polling: text.includes("The event stream was unavailable; polling for progress."),
        streaming: text.includes("Streaming live from the backend."),
      });
    })()`)) || "{}",
  );
  timeline.progress = peakRows;

  if (timeline.progress < MIN_PROGRESS_EVENTS) {
    findings.push(
      `the console rendered ${timeline.progress} progress row(s); expected at ` +
        `least ${MIN_PROGRESS_EVENTS}. Events reached neither the page nor the ` +
        `person watching it.`,
    );
  } else {
    note(`${timeline.progress} progress rows rendered on screen`);
  }

  if (pollRequests().length > MAX_TOLERATED_POLLS) {
    findings.push(
      `progress was carried by the polling fallback: ${pollRequests().length} ` +
        `requests to /status against ${streamRequests().length} to /events. The ` +
        `fallback working is what hid F29 for the console's whole life — the ` +
        `only symptom was a tag nobody reads.`,
    );
  }
}

// --- 5. Download the report, and look at the bytes ------------------------
//
// F30: these were `<a href>` links straight at the API, which cannot carry the
// credential, so the browser saved `{"detail":"Not authenticated"}` under the
// name of a report — a file that opens as a broken PDF and explains nothing.
let saved = null;
if (investigationId) {
  await send("Page.navigate", { url: `${BASE}/reports` });
  await settle(3000);

  // **Look at the control before clicking it**, because the shipped shape of
  // F30 is invisible to everything downstream. An `<a href target="_blank">`
  // at the API opens no popup in headless Chrome, so the click produces no
  // request, no target event and no file — the run can only report that
  // nothing arrived, which is indistinguishable from a broken download
  // directory. The mechanism *is* the defect: a navigation cannot carry the
  // Authorization header these routes require, so naming an anchor pointed at
  // the API is both deterministic and the most direct thing to say.
  const control = JSON.parse(
    (await evaluate(`(() => {
      const el = [...document.querySelectorAll("button, a")]
        .find(el => /^(PDF|PDF Report)$/i.test(el.textContent.trim()));
      if (!el) return JSON.stringify({ found: false });
      return JSON.stringify({
        found: true,
        tag: el.tagName,
        href: el.getAttribute("href") || "",
      });
    })()`)) || '{"found":false}',
  );

  if (
    control.found &&
    control.tag === "A" &&
    /\/investigations\/[^/]+\/(pdf|json|markdown)/.test(control.href)
  ) {
    findings.push(
      `the PDF control is an <a href> pointed at ${control.href} — a browser ` +
        `navigation, which cannot carry the Authorization header these routes ` +
        `require. It is answered 401 and the browser saves the error body under ` +
        `the name of a report (F30). Fetch it with the credential instead.`,
    );
  }

  const asked =
    control.found &&
    (await evaluate(`(() => {
    const el = [...document.querySelectorAll("button, a")]
      .find(el => /^(PDF|PDF Report)$/i.test(el.textContent.trim()));
    if (!el) return false;
    el.click();
    return true;
  })()`));

  if (!asked) {
    refusals.push(
      "no PDF control was found on /reports, so nothing was downloaded and the " +
        "report assertions describe nothing.",
    );
  } else {
    await until(`true`, 1).catch(() => {});
    // **Only files that are reports.** Chrome writes its own things into a
    // download directory — a run with the stream mutated produced a
    // `downloads.html` whose first bytes are `Cr24`, the CRX magic — and
    // taking the first file in the directory reported that as "the downloaded
    // report is not a PDF". A false positive in a required job is the failure
    // this harness exists to prevent, not to commit: a check that cries wolf
    // gets skipped exactly like a flaky one.
    //
    // The platform names every report `investigation-<id>.<ext>`, and the
    // console falls back to `investigation.pdf` when a proxy strips the
    // header, so both spellings count and nothing else does.
    const isReport = (name) =>
      /^investigation[-.]/.test(name) && !name.endsWith(".crdownload");
    const deadline = Date.now() + 20000;
    while (Date.now() < deadline && !readdirSync(downloadDir).some(isReport)) {
      await settle(500);
    }
    const files = readdirSync(downloadDir).filter(isReport);
    const reportResponses = seen(
      /\/investigations\/[^/]+\/(pdf|json|markdown)/,
    );
    const refusedReports = reportResponses.filter((r) => r.status !== 200);

    if (refusedReports.length) {
      findings.push(
        `the report request was refused: ${refusedReports.map((r) => r.status).join(", ")}. ` +
          `An <a href> cannot send an Authorization header, so this is 401 in ` +
          `every authenticated deployment and the browser saves the error body ` +
          `under the name of a report (F30).`,
      );
    }

    if (!files.length) {
      if (!refusedReports.length && !findings.length) {
        refusals.push(
          "the PDF control was clicked but no file arrived, and no report " +
            "request was refused either — so this says nothing about whether " +
            "downloads work. Check Chrome's download behaviour.",
        );
      }
    } else {
      const path = join(downloadDir, files[0]);
      const bytes = readFileSync(path);
      saved = {
        name: files[0],
        size: statSync(path).size,
        magic: bytes.subarray(0, 5).toString(),
      };

      if (!saved.magic.startsWith("%PDF")) {
        findings.push(
          `the downloaded report is not a PDF: ${saved.name} begins ` +
            `${JSON.stringify(saved.magic)} and is ${saved.size} bytes. This is ` +
            `the shape F30 shipped — a JSON error body saved under a report's ` +
            `name, which opens as a broken document and explains nothing.`,
        );
      } else if (saved.size < MIN_REPORT_BYTES) {
        findings.push(
          `the downloaded PDF is only ${saved.size} bytes, which is too small ` +
            `to be an incident report.`,
        );
      } else {
        note(`downloaded ${saved.name} — ${saved.size} bytes, begins %PDF`);
      }
    }
  }
}

// --- 6. Was the investigation worth asserting about? ----------------------
//
// The repo's standing rule: a `succeeded` investigation that collected nothing
// is refused. Two providers that failed identically, or a cluster that answered
// nothing, produce a beautiful run that measured the harness.
if (investigationId) {
  try {
    const response = await fetch(`${API}/investigations/${investigationId}`, {
      headers: TOKEN ? { Authorization: `Bearer ${TOKEN}` } : {},
    });
    const job = await response.json();
    const coverage = job?.investigation?.evidence_coverage || {};
    const usable = Number(coverage.usable ?? 0);
    if (!usable) {
      // Counts and one example, not the whole coverage block: this refusal is
      // read by someone whose run just looked perfect, and eleven identical
      // `forbidden` details bury the one sentence that explains it.
      const byStatus = coverage.by_status || {};
      const example = (coverage.degraded || [])[0];
      refusals.push(
        `the investigation collected no usable evidence — ${coverage.total ?? 0} ` +
          `records, ${JSON.stringify(byStatus)}` +
          (example ? `, e.g. ${example.kind}: ${example.detail}` : "") +
          `. The console rendered it perfectly and there was nothing behind it, ` +
          `which is the most convincing way for this journey to be worthless. ` +
          `Grant the caller Kubernetes RBAC — impersonation is on by default, so ` +
          `an unbound subject has every read refused.`,
      );
    } else {
      note(`the investigation collected ${usable} usable evidence records`);
    }
  } catch (error) {
    refusals.push(
      `could not read the investigation back from ${API}: ${error.message}`,
    );
  }
}

ws.close();

/* --------------------------------------------------------------- verdict -- */

console.log("");
for (const finding of findings) console.log(`FAIL  ${finding}\n`);
for (const refusal of refusals) console.log(`REFUSED  ${refusal}\n`);

console.log(
  `journey: ${findings.length} finding(s), ${refusals.length} refusal(s)` +
    `  [stream ${streamRequests().length} req, poll ${pollRequests().length} req, ` +
    `${timeline.progress} rows, ${saved ? `${saved.size}B ${saved.magic.slice(0, 4)}` : "no file"}]`,
);

if (consoleErrors.length) {
  console.log(`\nconsole errors seen (not failing this check):`);
  for (const error of [...new Set(consoleErrors)].slice(0, 5))
    console.log(`  ${error}`);
}

if (refusals.length) process.exit(2);
process.exit(findings.length ? 1 : 0);
