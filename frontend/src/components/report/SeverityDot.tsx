import type { SeverityTone } from "../../lib/report";

/**
 * Severity, encoded three ways: glyph, colour, and label.
 *
 * Never colour alone. Red/green is the most common colour-vision deficiency
 * and severity is the most important signal in the product, so it also has to
 * survive greyscale — these views become PDF postmortems.
 */
const GLYPH: Record<SeverityTone, string> = {
  critical: "●",
  warning: "◐",
  healthy: "○",
  neutral: "◌",
};

const COLOUR: Record<SeverityTone, string> = {
  critical: "text-critical",
  warning: "text-warning",
  healthy: "text-healthy",
  neutral: "text-ink-3",
};

export function SeverityDot({
  tone,
  label,
  className = "",
}: {
  tone: SeverityTone;
  label?: string;
  className?: string;
}) {
  return (
    <span className={`inline-flex items-center gap-1.5 whitespace-nowrap ${COLOUR[tone]} ${className}`}>
      <span aria-hidden="true" className="text-[11px] leading-none">
        {GLYPH[tone]}
      </span>
      {label ? <span className="text-sm">{label}</span> : null}
    </span>
  );
}
