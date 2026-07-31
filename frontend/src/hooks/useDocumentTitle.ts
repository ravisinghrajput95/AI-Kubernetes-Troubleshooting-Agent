import { useEffect } from "react";

const SUFFIX = "Kubernetes Operations";

/**
 * Name the tab after what is in it.
 *
 * An operator working an incident ends up with several of these open at once —
 * a fleet, two investigations, a report. Identical tab titles make that pile
 * unnavigable, and the tab strip is the only place the distinction can appear.
 */
export function useDocumentTitle(title?: string): void {
  useEffect(() => {
    document.title = title ? `${title} · ${SUFFIX}` : SUFFIX;
    return () => {
      document.title = SUFFIX;
    };
  }, [title]);
}
