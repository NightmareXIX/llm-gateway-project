import type { ReactNode } from "react";

import { cn } from "@/lib/cn";

/**
 * One labelled horizontal bar: a name, a magnitude, and a number beside it.
 *
 * The bar is an inline SVG rather than a `div` with a percentage width for the
 * same reason `Sparkline` is one (D51) — it is a picture of a number, and
 * keeping every chart in this dashboard the same primitive means one place
 * gets the zero-denominator guard right instead of three.
 *
 * **`max <= 0` renders an empty track, never a `NaN` width.** A dashboard's
 * first day is a day with no traffic, and `value / 0` reaching an SVG
 * attribute is the fastest way for that day to look broken rather than quiet.
 */
export function BarRow({
  label,
  sublabel,
  value,
  max,
  valueLabel,
  tone = "accent",
  className,
}: {
  label: ReactNode;
  sublabel?: ReactNode;
  value: number;
  /** The largest value among the sibling rows — the scale they share. */
  max: number;
  /** What to print at the end of the row. The bar shows proportion; this shows
   *  the actual quantity, because a bar alone cannot be read off. */
  valueLabel: ReactNode;
  tone?: "accent" | "warn";
  className?: string;
}) {
  const fraction = max > 0 ? Math.min(1, Math.max(0, value / max)) : 0;

  return (
    <div className={cn("space-y-1", className)}>
      <div className="flex items-baseline justify-between gap-3 text-xs">
        <span className="min-w-0 truncate font-medium text-ink">{label}</span>
        <span className="shrink-0 font-mono text-ink-secondary">{valueLabel}</span>
      </div>
      <svg
        role="img"
        aria-label={`${labelText(label)}: ${Math.round(fraction * 100)}% of the largest`}
        viewBox="0 0 100 6"
        preserveAspectRatio="none"
        className="h-1.5 w-full"
      >
        <rect x="0" y="0" width="100" height="6" rx="3" className="fill-sunken" />
        {fraction > 0 && (
          <rect
            x="0"
            y="0"
            width={(fraction * 100).toFixed(2)}
            height="6"
            rx="3"
            className={tone === "warn" ? "fill-warn" : "fill-accent"}
          />
        )}
      </svg>
      {sublabel && <p className="text-[0.6875rem] text-ink-tertiary">{sublabel}</p>}
    </div>
  );
}

/** The accessible name needs a string; a `ReactNode` label that is not one
 *  falls back to nothing rather than to `"[object Object]"`. */
function labelText(label: ReactNode): string {
  return typeof label === "string" || typeof label === "number" ? String(label) : "";
}
