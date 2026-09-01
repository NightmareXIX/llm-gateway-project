import { cn } from "@/lib/cn";

/**
 * A rate, as a percentage and a bar: `value` out of `total`.
 *
 * **The denominator is guarded here rather than at every call site**, and that
 * is the whole reason this is a component. Three rates share this shape (error,
 * cache hit, failover) and all three have the same zero denominator on a
 * brand-new account; a dashboard whose first impression is `NaN%` is the
 * failure this component exists to make unrepresentable.
 *
 * A zero denominator renders an em dash and an empty track — *no data*, which
 * is a different statement from `0%` (a real rate over real traffic) and is
 * printed differently on purpose.
 */
export function Meter({
  label,
  value,
  total,
  hint,
  tone = "accent",
  className,
}: {
  label: string;
  value: number;
  total: number;
  /** One short line under the number: what it is over, in words. */
  hint?: string;
  tone?: "accent" | "warn" | "danger";
  className?: string;
}) {
  const known = total > 0;
  const fraction = known ? Math.min(1, Math.max(0, value / total)) : 0;

  return (
    <div className={cn("rounded-control border border-subtle bg-raised p-3", className)}>
      <p className="text-xs text-ink-tertiary">{label}</p>
      <p className="mt-0.5 text-xl font-medium text-ink tabular-nums">
        {known ? formatPercent(fraction) : "—"}
      </p>
      <svg
        role="img"
        aria-label={known ? `${label}: ${formatPercent(fraction)}` : `${label}: no data`}
        viewBox="0 0 100 4"
        preserveAspectRatio="none"
        className="mt-2 h-1 w-full"
      >
        <rect x="0" y="0" width="100" height="4" rx="2" className="fill-sunken" />
        {fraction > 0 && (
          <rect
            x="0"
            y="0"
            width={(fraction * 100).toFixed(2)}
            height="4"
            rx="2"
            className={TONES[tone]}
          />
        )}
      </svg>
      {hint && <p className="mt-1.5 text-[0.6875rem] text-ink-tertiary">{hint}</p>}
    </div>
  );
}

const TONES = {
  accent: "fill-accent",
  warn: "fill-warn",
  danger: "fill-danger",
} as const;

/**
 * Whole percent, except where that would round a real rate down to nothing.
 *
 * A rate under 1% keeps one significant digit — "0.4%", "0.04%" — because a
 * small error rate is a different fact from no errors at all, and rounding it
 * to `0%` rounds in the flattering direction. Everything at or above 1% is a
 * whole number: a dashboard reading "6.0%" is only pretending to a precision
 * the underlying counts do not have.
 */
export function formatPercent(fraction: number): string {
  const percent = fraction * 100;
  if (percent === 0) return "0%";
  if (percent < 1) return `${Number(percent.toPrecision(1))}%`;
  return `${Math.round(percent)}%`;
}
