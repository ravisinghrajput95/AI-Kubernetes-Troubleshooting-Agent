import { HistoryTable } from "../App";

/**
 * Saved investigation reports.
 *
 * The first route split: this table lived at the bottom of the single scrolling
 * page, below the investigation form that produced it. Phase 3 moves the
 * component itself out of `App.tsx`; this phase only gives it somewhere to be.
 */
export function ReportsPage() {
  return (
    <div className="mx-auto max-w-document px-6 py-8">
      <h1 className="text-h1">Reports</h1>
      <p className="mt-2 max-w-measure text-sm leading-6 text-ink-2">
        Completed investigations, saved as incident reports.
      </p>
      <div className="mt-6">
        <HistoryTable />
      </div>
    </div>
  );
}
