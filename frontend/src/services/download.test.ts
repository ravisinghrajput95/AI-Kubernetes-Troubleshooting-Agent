/**
 * Report downloads must carry the credential.
 *
 * The console linked all three formats with `<a href>` straight at the API.
 * A plain navigation cannot send an `Authorization` header and every report
 * route is behind `require_principal`, so in any deployment with
 * authentication configured — which is all of them — clicking "PDF Report"
 * was answered 401 and saved a JSON error body under the name of a report.
 * Measured against a token deployment: pdf, json and markdown all 401 as a
 * browser sends them, all 200 with the header.
 *
 * Same root cause as the progress stream, which was an `EventSource` for the
 * same reason and refused for the same one — so the two are tested the same
 * way, on the request that actually goes out.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { downloadReport } from "./api";
import { clearToken, setToken } from "./auth";
import { filenameFrom } from "../lib/download";

const saved: Array<{ name: string; size: number }> = [];

beforeEach(() => {
  saved.length = 0;
  setToken("report-test-token");
  // `saveBlob` builds an object URL and clicks an anchor; jsdom has neither.
  vi.stubGlobal("URL", {
    ...URL,
    createObjectURL: () => "blob:stub",
    revokeObjectURL: () => {},
  });
  vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(function (
    this: HTMLAnchorElement,
  ) {
    saved.push({ name: this.download, size: 1 });
  });
});

afterEach(() => {
  clearToken();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

function respondWith(status: number, disposition?: string): typeof fetch {
  return vi.fn().mockResolvedValue({
    ok: status === 200,
    status,
    headers: { get: (name: string) => (name === "Content-Disposition" ? disposition : null) },
    blob: async () => new Blob(["report bytes"]),
  }) as unknown as typeof fetch;
}

describe("downloadReport", () => {
  it("sends the Authorization header an <a href> could not", async () => {
    const fetchImpl = respondWith(200, 'attachment; filename="investigation-abc.pdf"');
    vi.stubGlobal("fetch", fetchImpl);

    await downloadReport("/investigations/abc/pdf", "investigation.pdf");

    expect(fetchImpl).toHaveBeenCalledWith(
      expect.stringContaining("/investigations/abc/pdf"),
      expect.objectContaining({
        headers: expect.objectContaining({ Authorization: "Bearer report-test-token" }),
      }),
    );
  });

  it("saves under the name the server asked for", async () => {
    vi.stubGlobal("fetch", respondWith(200, 'attachment; filename="investigation-abc.pdf"'));

    await downloadReport("/investigations/abc/pdf", "fallback.pdf");

    // The platform names every report; composing a second name here would be
    // a second namer to keep in step.
    expect(saved).toEqual([{ name: "investigation-abc.pdf", size: 1 }]);
  });

  it("raises rather than saving an error body under a report's name", async () => {
    // Exactly what shipped: a 401 whose JSON body was written to disk as
    // `investigation-<id>.pdf`, which opens as a broken PDF and says nothing.
    vi.stubGlobal("fetch", respondWith(401));

    await expect(downloadReport("/investigations/abc/pdf", "investigation.pdf")).rejects.toThrow();
    expect(saved).toEqual([]);
  });

  it("explains a pruned report rather than reporting it as a failure", async () => {
    vi.stubGlobal("fetch", respondWith(404));

    await expect(
      downloadReport("/investigations/abc/pdf", "investigation.pdf"),
    ).rejects.toThrow(/retention/i);
  });
});

describe("filenameFrom", () => {
  it("reads the filename out of a Content-Disposition", () => {
    expect(filenameFrom('attachment; filename="investigation-abc.pdf"', "x.pdf")).toBe(
      "investigation-abc.pdf",
    );
  });

  it("falls back when a proxy strips the header", () => {
    // Costing the suggested name is acceptable; costing the download is not.
    expect(filenameFrom(null, "investigation.pdf")).toBe("investigation.pdf");
  });
});
