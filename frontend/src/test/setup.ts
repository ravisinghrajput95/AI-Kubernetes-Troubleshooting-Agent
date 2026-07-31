// React needs this flag to treat updates as act-wrapped in the test environment.
(globalThis as unknown as { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT =
  true;

// DOM matchers (`toBeInTheDocument`, `toBeDisabled`, …) for component tests.
// The console had no component tests before Phase 0 — it is listed as a gap in
// docs/PRODUCTION_READINESS.md — so this arrives with the first of them.
import "@testing-library/jest-dom/vitest";
