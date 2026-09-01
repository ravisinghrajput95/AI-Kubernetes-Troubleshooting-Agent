export function StatusPill({
  label,
  tone = "neutral",
}: {
  label: string;
  tone?: "neutral" | "good" | "warning" | "info";
}) {
  const classes = {
    neutral: "border-slate-700 bg-slate-900 text-slate-300",
    good: "border-lime-800 bg-lime-950/40 text-lime-300",
    warning: "border-amber-800 bg-amber-950/40 text-amber-300",
    info: "border-sky-800 bg-sky-950/40 text-sky-300",
  };

  return (
    <span
      className={`inline-flex items-center rounded-md border px-3 py-1.5 text-xs font-semibold ${classes[tone]}`}
    >
      {label}
    </span>
  );
}
