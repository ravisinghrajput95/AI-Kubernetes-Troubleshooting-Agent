import type { EvidenceEntry } from "../../types/investigation";

/**
 * The evidence a claim rests on, at the point of the claim.
 *
 * This is the primitive the product turns on. Conclusions already reference
 * evidence ids rather than copying payloads — that is the citation spine, and
 * it has until now been a backend property an operator had to go looking for.
 * Inline, it answers "why should I believe you?" in zero navigation.
 *
 * A real `<button>`, so it is in the tab order and reachable without a mouse.
 */
export function CitationChip({
  index,
  evidence,
  active,
  onSelect,
}: {
  /** Position within a claim's citations. Omit where a figure has exactly one:
   *  a number that is always "1" distinguishes nothing and only adds noise. */
  index?: number;
  evidence?: EvidenceEntry;
  active: boolean;
  onSelect: () => void;
}) {
  const label = evidence
    ? `Evidence${index ? ` ${index}` : ""}: ${evidence.kind}${
        evidence.command ? ` — ${evidence.command}` : ""
      }`
    : `Evidence${index ? ` ${index}` : ""}`;

  return (
    <button
      type="button"
      onClick={onSelect}
      aria-pressed={active}
      title={label}
      className={`mx-0.5 inline-flex translate-y-[1px] items-center rounded border px-1 align-baseline font-mono text-sm leading-tight transition-colors duration-fast focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-1 focus-visible:outline-info ${
        active
          ? "border-info bg-info/25 text-ink"
          : "border-info/30 bg-info/10 text-info hover:bg-info/20"
      }`}
    >
      <span className="sr-only">{label}</span>
      <span aria-hidden="true">{index ?? "\u00b7\u00b7"}</span>
    </button>
  );
}

/** The chips for one claim. Renders nothing when a claim cites nothing. */
export function Citations({
  ids,
  index,
  selected,
  onSelect,
}: {
  ids: string[];
  index: Map<string, EvidenceEntry>;
  selected: string;
  onSelect: (id: string) => void;
}) {
  if (ids.length === 0) {
    return null;
  }
  return (
    <>
      {ids.map((id, position) => (
        <CitationChip
          key={id}
          index={position + 1}
          evidence={index.get(id)}
          active={id === selected}
          onSelect={() => onSelect(id)}
        />
      ))}
    </>
  );
}
