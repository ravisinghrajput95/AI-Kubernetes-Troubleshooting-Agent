import { useCallback } from "react";
import { useSearchParams } from "react-router";

/**
 * The cluster the current view is scoped to, held in the URL.
 *
 * In the URL rather than React state so that scope is shareable and survives a
 * reload — the same reason investigations get their own route. A colleague
 * pasted `?cluster=prod-eu-west` sees what you were looking at.
 */
export function useScope(): {
  cluster: string;
  setCluster: (next: string) => void;
} {
  const [params, setParams] = useSearchParams();

  const setCluster = useCallback(
    (next: string) => {
      setParams(
        (current) => {
          const updated = new URLSearchParams(current);
          if (next) {
            updated.set("cluster", next);
          } else {
            updated.delete("cluster");
          }
          return updated;
        },
        // Switching scope is navigation within a view, not a new destination;
        // it should not stack up entries the back button has to walk through.
        { replace: true },
      );
    },
    [setParams],
  );

  return { cluster: params.get("cluster") ?? "", setCluster };
}
