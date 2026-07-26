/**
 * Exercises the real fetch transport against a running backend.
 *
 * Unit tests mock `fetch`, which proves the logic but not the wire. This closes
 * that gap: real HTTP, real FastAPI error bodies, real JSON shapes.
 *
 * Opt-in, so the default suite stays hermetic:
 *   VITE_API_INTEGRATION=1 react_PUBLIC_API_BASE_URL=http://127.0.0.1:8778 npm test
 */

import { describe, expect, it } from "vitest";

import {
  cancelInvestigationJob,
  getHealth,
  getInvestigationJob,
  getInvestigationReport,
  startInvestigationJob,
} from "./api";
import { ApiError } from "./http";

const enabled = Boolean(import.meta.env.VITE_API_INTEGRATION);

describe.skipIf(!enabled)("http transport against a live backend", () => {
  it("reads a JSON response", async () => {
    await expect(getHealth()).resolves.toMatchObject({ status: "healthy" });
  });

  it("posts a body and receives the accepted job", async () => {
    const accepted = await startInvestigationJob("test-cluster", {
      namespace: "prod",
    });

    expect(accepted.id).toBeTruthy();
    expect(accepted.status).toBe("pending");
    expect(accepted.events_url).toBe(`/investigations/${accepted.id}/events`);
  });

  it("drives a job to completion and reads the result", async () => {
    const accepted = await startInvestigationJob("test-cluster");

    const deadline = Date.now() + 60_000;
    let state = await getInvestigationJob(accepted.id);
    while (Date.now() < deadline && !["succeeded", "failed", "cancelled"].includes(state.status)) {
      await new Promise((resolve) => setTimeout(resolve, 300));
      state = await getInvestigationJob(accepted.id);
    }

    expect(state.status).toBe("succeeded");
    expect(state.diagnosis?.root_cause).toBeTruthy();
    expect(state.investigation?.evidence?.length).toBeGreaterThan(0);

    // The job id doubles as the report id.
    await expect(getInvestigationReport(accepted.id)).resolves.toMatchObject({
      status: "success",
    });
  }, 90_000);

  it("surfaces a FastAPI detail message on 404", async () => {
    const error = await getInvestigationJob("does-not-exist").catch((err) => err);

    expect(error).toBeInstanceOf(ApiError);
    expect(error.status).toBe(404);
    expect(error.message).toContain("not found");
  });

  it("surfaces a 409 when cancelling a finished job", async () => {
    const accepted = await startInvestigationJob("test-cluster");

    const deadline = Date.now() + 60_000;
    let state = await getInvestigationJob(accepted.id);
    while (Date.now() < deadline && state.status !== "succeeded") {
      await new Promise((resolve) => setTimeout(resolve, 300));
      state = await getInvestigationJob(accepted.id);
    }

    const error = await cancelInvestigationJob(accepted.id).catch((err) => err);
    expect(error).toBeInstanceOf(ApiError);
    expect(error.status).toBe(409);
  }, 90_000);
});
