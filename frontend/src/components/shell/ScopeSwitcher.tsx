import { useEffect, useRef, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { Check, ChevronsUpDown } from "lucide-react";

import { getKubernetesContexts } from "../../services/api";
import { useScope } from "../../hooks/useScope";

/**
 * Which cluster the current view is scoped to.
 *
 * In the header, the way Vercel scopes to a project and Stripe to an account —
 * not a 320px navigation column. Selecting a cluster filters the current view
 * rather than navigating, so the operator keeps their place.
 */
export function ScopeSwitcher() {
  const { cluster, setCluster } = useScope();
  const { data, isLoading } = useQuery({
    queryKey: ["kubernetes-contexts"],
    queryFn: getKubernetesContexts,
  });

  const [open, setOpen] = useState(false);
  const containerRef = useRef<HTMLDivElement>(null);
  const buttonRef = useRef<HTMLButtonElement>(null);

  const contexts = data?.items ?? [];

  // Adopt the kubeconfig's current context when nothing is scoped yet, so the
  // console opens somewhere useful rather than empty.
  useEffect(() => {
    if (!cluster && data?.current_context) {
      setCluster(data.current_context);
    }
  }, [cluster, data?.current_context, setCluster]);

  useEffect(() => {
    if (!open) {
      return;
    }
    function onPointerDown(event: MouseEvent) {
      if (!containerRef.current?.contains(event.target as Node)) {
        setOpen(false);
      }
    }
    function onKeyDown(event: KeyboardEvent) {
      if (event.key === "Escape") {
        setOpen(false);
        buttonRef.current?.focus();
      }
    }
    document.addEventListener("mousedown", onPointerDown);
    document.addEventListener("keydown", onKeyDown);
    return () => {
      document.removeEventListener("mousedown", onPointerDown);
      document.removeEventListener("keydown", onKeyDown);
    };
  }, [open]);

  return (
    <div ref={containerRef} className="relative">
      <button
        ref={buttonRef}
        type="button"
        onClick={() => setOpen((current) => !current)}
        aria-haspopup="listbox"
        aria-expanded={open}
        className="flex max-w-[280px] items-center gap-2 rounded-md border border-line bg-raised px-2.5 py-1.5 text-sm transition-colors duration-fast hover:border-ink-3 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-info"
      >
        <span className="truncate">
          {cluster || (isLoading ? "Loading clusters…" : "Select a cluster")}
        </span>
        <ChevronsUpDown aria-hidden="true" className="size-3.5 shrink-0 text-ink-3" />
      </button>

      {open ? (
        <ul
          role="listbox"
          aria-label="Clusters"
          className="absolute left-0 top-full z-30 mt-1 max-h-80 w-[320px] overflow-y-auto rounded-md border border-line bg-overlay p-1 shadow-xl shadow-black/40"
        >
          {contexts.length === 0 ? (
            <li className="px-2.5 py-2 text-sm text-ink-3">
              {data?.error ?? "No kubeconfig contexts found."}
            </li>
          ) : null}
          {contexts.map((context) => (
            <li key={context.name}>
              <button
                type="button"
                role="option"
                aria-selected={context.name === cluster}
                onClick={() => {
                  setCluster(context.name);
                  setOpen(false);
                  buttonRef.current?.focus();
                }}
                className="flex w-full items-center gap-2 rounded px-2.5 py-2 text-left text-sm transition-colors duration-fast hover:bg-raised focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-[-2px] focus-visible:outline-info"
              >
                <Check
                  aria-hidden="true"
                  className={`size-3.5 shrink-0 ${
                    context.name === cluster ? "text-info" : "opacity-0"
                  }`}
                />
                <span className="min-w-0 flex-1">
                  <span className="block truncate">{context.name}</span>
                  <span className="block truncate text-sm text-ink-3">{context.cluster}</span>
                </span>
                {context.current ? (
                  <span className="shrink-0 text-label uppercase text-ink-3">default</span>
                ) : null}
              </button>
            </li>
          ))}
        </ul>
      ) : null}
    </div>
  );
}
