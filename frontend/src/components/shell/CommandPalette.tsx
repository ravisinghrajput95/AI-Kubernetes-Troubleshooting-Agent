import { useEffect, useMemo, useRef, useState } from "react";
import { useNavigate } from "react-router";

import { DESTINATIONS } from "./NavRail";
import { getKubernetesContexts } from "../../services/api";
import { useScope } from "../../hooks/useScope";
import { signOut } from "../../services/auth";
import { useQuery } from "@tanstack/react-query";

/**
 * ⌘K — the primary navigation for the target user, not a garnish.
 *
 * Incident work is keyboard work, and an operator aiming a mouse at 02:41 is
 * being slowed down. Hand-rolled rather than pulled from a component library:
 * it is a listbox and a filter, and the accessibility burden here is small
 * enough to carry directly.
 */

export interface Command {
  id: string;
  label: string;
  hint?: string;
  group: string;
  run: () => void;
}

export function CommandPalette({
  open,
  onClose,
  onStartInvestigation,
}: {
  open: boolean;
  onClose: () => void;
  onStartInvestigation?: () => void;
}) {
  const navigate = useNavigate();
  const { cluster, setCluster } = useScope();
  const [query, setQuery] = useState("");
  const [active, setActive] = useState(0);
  const inputRef = useRef<HTMLInputElement>(null);
  const previousFocus = useRef<Element | null>(null);

  const { data } = useQuery({
    queryKey: ["kubernetes-contexts"],
    queryFn: getKubernetesContexts,
    enabled: open,
  });

  const commands = useMemo<Command[]>(() => {
    const go: Command[] = DESTINATIONS.map((destination) => ({
      id: `go:${destination.to}`,
      label: destination.label,
      hint: `g ${destination.chord}`,
      group: "Go to",
      run: () => navigate(destination.to),
    }));

    const clusters: Command[] = (data?.items ?? []).map((context) => ({
      id: `cluster:${context.name}`,
      label: context.name,
      hint: context.name === cluster ? "current" : undefined,
      group: "Scope to cluster",
      run: () => setCluster(context.name),
    }));

    const actions: Command[] = [
      ...(onStartInvestigation
        ? [
            {
              id: "action:investigate",
              label: "Start an investigation",
              hint: "i",
              group: "Actions",
              run: onStartInvestigation,
            },
          ]
        : []),
      {
        id: "action:signout",
        label: "Sign out",
        group: "Actions",
        run: signOut,
      },
    ];

    return [...actions, ...go, ...clusters];
  }, [cluster, data?.items, navigate, onStartInvestigation, setCluster]);

  const matches = useMemo(() => {
    const needle = query.trim().toLowerCase();
    if (!needle) {
      return commands;
    }
    return commands.filter(
      (command) =>
        command.label.toLowerCase().includes(needle) ||
        command.group.toLowerCase().includes(needle),
    );
  }, [commands, query]);

  useEffect(() => {
    setActive(0);
  }, [query, open]);

  useEffect(() => {
    if (!open) {
      return;
    }
    previousFocus.current = document.activeElement;
    inputRef.current?.focus();
    return () => {
      // Focus returns where it came from; losing it to the page body is a
      // keyboard user's equivalent of being teleported.
      (previousFocus.current as HTMLElement | null)?.focus?.();
    };
  }, [open]);

  if (!open) {
    return null;
  }

  function choose(command: Command | undefined) {
    if (!command) {
      return;
    }
    command.run();
    onClose();
  }

  function onKeyDown(event: React.KeyboardEvent) {
    if (event.key === "Escape") {
      event.preventDefault();
      onClose();
      return;
    }
    if (event.key === "ArrowDown") {
      event.preventDefault();
      setActive((current) => (matches.length ? (current + 1) % matches.length : 0));
      return;
    }
    if (event.key === "ArrowUp") {
      event.preventDefault();
      setActive((current) =>
        matches.length ? (current - 1 + matches.length) % matches.length : 0,
      );
      return;
    }
    if (event.key === "Enter") {
      event.preventDefault();
      choose(matches[active]);
    }
  }

  let renderedGroup = "";

  return (
    <div
      className="fixed inset-0 z-50 flex items-start justify-center bg-black/60 p-4 pt-[12vh]"
      onMouseDown={(event) => {
        if (event.target === event.currentTarget) {
          onClose();
        }
      }}
    >
      <div
        role="dialog"
        aria-modal="true"
        aria-label="Command palette"
        onKeyDown={onKeyDown}
        className="w-full max-w-xl overflow-hidden rounded-lg border border-line bg-overlay shadow-2xl shadow-black/50"
      >
        <input
          ref={inputRef}
          value={query}
          onChange={(event) => setQuery(event.target.value)}
          placeholder="Search clusters, pages and actions…"
          aria-label="Search commands"
          aria-controls="command-results"
          className="w-full border-b border-line bg-transparent px-4 py-3 text-body text-ink outline-none placeholder:text-ink-3"
        />

        <ul id="command-results" role="listbox" className="max-h-80 overflow-y-auto p-1.5">
          {matches.length === 0 ? (
            <li className="px-3 py-6 text-center text-sm text-ink-3">
              Nothing matches “{query}”.
            </li>
          ) : null}

          {matches.map((command, index) => {
            const showGroup = command.group !== renderedGroup;
            renderedGroup = command.group;
            return (
              <li key={command.id}>
                {showGroup ? (
                  <p className="px-3 pb-1 pt-3 text-label uppercase text-ink-3">
                    {command.group}
                  </p>
                ) : null}
                <button
                  type="button"
                  role="option"
                  aria-selected={index === active}
                  onMouseEnter={() => setActive(index)}
                  onClick={() => choose(command)}
                  className={`flex w-full items-center justify-between gap-3 rounded px-3 py-2 text-left text-sm transition-colors duration-fast ${
                    index === active ? "bg-raised text-ink" : "text-ink-2"
                  }`}
                >
                  <span className="min-w-0 truncate">{command.label}</span>
                  {command.hint ? (
                    <kbd className="shrink-0 font-mono text-sm text-ink-3">{command.hint}</kbd>
                  ) : null}
                </button>
              </li>
            );
          })}
        </ul>
      </div>
    </div>
  );
}
