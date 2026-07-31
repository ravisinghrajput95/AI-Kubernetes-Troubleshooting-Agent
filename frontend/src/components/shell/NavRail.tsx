import { useCallback, useEffect, useState } from "react";
import { NavLink } from "react-router";
import { FileText, LayoutGrid, PanelLeft, Search, Settings, Siren } from "lucide-react";

/**
 * Primary navigation.
 *
 * A rail, not a sidebar: 56px collapsed, 224px expanded, and the choice is
 * remembered. The column it replaces spent 320px — 18% of a 1440px screen — on
 * a control used once per session. The content area is the product.
 *
 * The rail is deliberately short. Destinations appear here only when they have
 * data behind them, so it grows as later phases land rather than shipping now
 * with items that lead to fabricated screens. See docs/CONSOLE_REDESIGN.md §0.
 */

const COLLAPSED_KEY = "k8s-agent-rail-collapsed";

export interface NavDestination {
  to: string;
  label: string;
  icon: typeof Siren;
  /** Second key of the `g` chord that jumps here. */
  chord: string;
}

export const DESTINATIONS: NavDestination[] = [
  // Fleet is first and Fleet is `/`. An enterprise operator's mental model is
  // a fleet of clusters they are accountable for; naming the default route
  // after that model is the cheapest way to say this is a fleet product.
  { to: "/", label: "Fleet", icon: LayoutGrid, chord: "f" },
  { to: "/investigations", label: "Investigations", icon: Siren, chord: "i" },
  { to: "/reports", label: "Reports", icon: FileText, chord: "r" },
  { to: "/settings", label: "Settings", icon: Settings, chord: "s" },
];

export function NavRail({ onOpenPalette }: { onOpenPalette: () => void }) {
  const [collapsed, setCollapsed] = useState(
    () => window.localStorage.getItem(COLLAPSED_KEY) === "1",
  );

  const toggle = useCallback(() => {
    setCollapsed((current) => {
      const next = !current;
      window.localStorage.setItem(COLLAPSED_KEY, next ? "1" : "0");
      return next;
    });
  }, []);

  useEffect(() => {
    document.documentElement.dataset.railCollapsed = collapsed ? "1" : "0";
  }, [collapsed]);

  return (
    <nav
      aria-label="Primary"
      className={`flex shrink-0 flex-col border-r border-line-muted bg-surface transition-[width] duration-base ${
        collapsed ? "w-14" : "w-56"
      }`}
    >
      <div className="flex h-14 items-center gap-2 px-3">
        <span
          aria-hidden="true"
          className="grid size-8 shrink-0 place-items-center rounded-md border border-line bg-raised text-info"
        >
          ◈
        </span>
        {!collapsed ? (
          <span className="truncate text-sm font-semibold">Kubernetes Ops</span>
        ) : null}
      </div>

      <button
        type="button"
        onClick={onOpenPalette}
        className={`mx-2 mb-2 flex items-center gap-2 rounded-md border border-line bg-raised px-2 py-1.5 text-sm text-ink-3 transition-colors duration-fast hover:border-ink-3 hover:text-ink-2 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-info ${
          collapsed ? "justify-center" : ""
        }`}
      >
        <Search aria-hidden="true" className="size-4 shrink-0" />
        {!collapsed ? (
          <>
            <span className="flex-1 text-left">Search</span>
            <kbd className="font-mono text-sm text-ink-3">⌘K</kbd>
          </>
        ) : null}
        <span className="sr-only">Open command palette</span>
      </button>

      <ul className="flex flex-1 flex-col gap-0.5 px-2">
        {DESTINATIONS.map(({ to, label, icon: Icon }) => (
          <li key={to}>
            <NavLink
              to={to}
              end={to === "/"}
              title={collapsed ? label : undefined}
              className={({ isActive }) =>
                `flex items-center gap-2.5 rounded-md px-2 py-2 text-sm transition-colors duration-fast focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-info ${
                  isActive
                    ? "bg-raised text-info"
                    : "text-ink-2 hover:bg-raised hover:text-ink"
                } ${collapsed ? "justify-center" : ""}`
              }
            >
              <Icon aria-hidden="true" className="size-4 shrink-0" />
              {!collapsed ? <span className="truncate">{label}</span> : null}
            </NavLink>
          </li>
        ))}
      </ul>

      <button
        type="button"
        onClick={toggle}
        aria-expanded={!collapsed}
        className={`m-2 flex items-center gap-2.5 rounded-md px-2 py-2 text-sm text-ink-3 transition-colors duration-fast hover:bg-raised hover:text-ink-2 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-info ${
          collapsed ? "justify-center" : ""
        }`}
      >
        <PanelLeft aria-hidden="true" className="size-4 shrink-0" />
        {!collapsed ? <span>Collapse</span> : null}
        <span className="sr-only">{collapsed ? "Expand navigation" : "Collapse navigation"}</span>
      </button>
    </nav>
  );
}
