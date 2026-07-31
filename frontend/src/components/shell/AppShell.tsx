import { useCallback, useEffect, useRef, useState } from "react";
import { Outlet, useNavigate } from "react-router";
import { useQuery } from "@tanstack/react-query";

import { CommandPalette } from "./CommandPalette";
import { DESTINATIONS, NavRail } from "./NavRail";
import { ScopeSwitcher } from "./ScopeSwitcher";
import { ErrorBoundary } from "../ErrorBoundary";
import { getHealth } from "../../services/api";

/**
 * The frame every page sits in: rail, header, content.
 *
 * Self-status lives here as a dot rather than as a dashboard tile. Giving the
 * tool's own health a card would put it at the same visual weight as the
 * customer's outage — see docs/CONSOLE_REDESIGN.md §21.
 */
export function AppShell() {
  const navigate = useNavigate();
  const [paletteOpen, setPaletteOpen] = useState(false);
  const chord = useRef<string>("");
  const chordTimer = useRef<number | null>(null);

  const { data: health, isError } = useQuery({
    queryKey: ["health"],
    queryFn: getHealth,
    retry: false,
    refetchInterval: 30_000,
  });

  const openPalette = useCallback(() => setPaletteOpen(true), []);

  useEffect(() => {
    function onKeyDown(event: KeyboardEvent) {
      const target = event.target as HTMLElement | null;
      const typing =
        target?.tagName === "INPUT" ||
        target?.tagName === "TEXTAREA" ||
        target?.tagName === "SELECT" ||
        target?.isContentEditable === true;

      if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "k") {
        event.preventDefault();
        setPaletteOpen((current) => !current);
        return;
      }

      // Bare keys must never fire while someone is typing into a field.
      if (typing || event.metaKey || event.ctrlKey || event.altKey) {
        return;
      }

      const key = event.key.toLowerCase();

      if (chord.current === "g") {
        chord.current = "";
        const destination = DESTINATIONS.find((item) => item.chord === key);
        if (destination) {
          event.preventDefault();
          navigate(destination.to);
        }
        return;
      }

      if (key === "g") {
        chord.current = "g";
        if (chordTimer.current !== null) {
          window.clearTimeout(chordTimer.current);
        }
        // A chord that never completes must not swallow the next keystroke.
        chordTimer.current = window.setTimeout(() => {
          chord.current = "";
        }, 1200);
      }
    }

    window.addEventListener("keydown", onKeyDown);
    return () => {
      window.removeEventListener("keydown", onKeyDown);
      if (chordTimer.current !== null) {
        window.clearTimeout(chordTimer.current);
      }
    };
  }, [navigate]);

  const offline = isError || health === undefined;

  return (
    <div className="flex min-h-screen bg-canvas text-ink">
      <NavRail onOpenPalette={openPalette} />

      <div className="flex min-w-0 flex-1 flex-col">
        <header className="sticky top-0 z-20 flex h-14 shrink-0 items-center justify-between gap-4 border-b border-line-muted bg-surface px-4">
          <ScopeSwitcher />

          <div className="flex items-center gap-3">
            <span
              className="flex items-center gap-2 text-sm text-ink-3"
              title={
                offline
                  ? "The backend could not be reached."
                  : `Connected to ${health?.service}`
              }
            >
              <span
                aria-hidden="true"
                className={`size-1.5 rounded-full ${offline ? "bg-critical" : "bg-healthy"}`}
              />
              {offline ? "Offline" : "Connected"}
            </span>
          </div>
        </header>

        <main className="min-w-0 flex-1">
          <ErrorBoundary>
            <Outlet />
          </ErrorBoundary>
        </main>
      </div>

      <CommandPalette open={paletteOpen} onClose={() => setPaletteOpen(false)} />
    </div>
  );
}
