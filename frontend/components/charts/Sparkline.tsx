import { cn } from "@/lib/cn";

/**
 * A time series as an inline SVG area, drawn from an array of numbers (D51).
 *
 * **No chart library.** A dependency for this would cost hundreds of kilobytes,
 * render through a canvas or a `ResizeObserver` that jsdom does not implement —
 * so its tests would assert on mocks rather than on output — and buy nothing a
 * fixed-width normalized polyline does not already give.
 *
 * The component is a pure function of its props and emits a fixed `viewBox`
 * scaled by CSS, so there is no measurement step and nothing to do on resize.
 * Two rules carry the honesty of the picture:
 *
 * - **Every bucket is a point, zeros included.** The server generates the
 *   buckets and left-joins the counts (D45, trap 8), so a quiet hour arrives
 *   as a `0` and is drawn as one. Nothing here may compact or skip a point:
 *   dropping zeros is exactly how a chart comes to draw a smooth line through
 *   an outage.
 * - **The scale is explicit and never inferred from a single value.** An
 *   all-zero series renders as a flat line on a baseline of 1, not as a full-
 *   height band that suggests traffic where there was none.
 */
export function Sparkline({
  values,
  overlay,
  ariaLabel,
  className,
}: {
  /** One number per bucket, oldest first. May be empty. */
  values: number[];
  /** An optional second series over the same buckets — errors under volume —
   *  drawn as a line on the same scale so the two are comparable by eye.
   *  Ignored when its length does not match `values`: two series on different
   *  bucket counts share no x-axis, and drawing them anyway would be a lie
   *  that looks like a chart. */
  overlay?: number[];
  ariaLabel: string;
  className?: string;
}) {
  const empty = values.length === 0;
  // Never zero: it is a divisor below, and an all-zero window is a real answer
  // that has to render as a flat floor rather than as `NaN` or as full height.
  const max = Math.max(1, ...values, ...(overlay ?? []));
  const showOverlay = overlay !== undefined && overlay.length === values.length && !empty;

  return (
    <svg
      role="img"
      aria-label={ariaLabel}
      data-empty={empty || undefined}
      viewBox={`0 0 ${VIEW_W} ${VIEW_H}`}
      preserveAspectRatio="none"
      className={cn("h-24 w-full", className)}
    >
      {empty ? (
        <line
          x1="0"
          y1={VIEW_H - 0.5}
          x2={VIEW_W}
          y2={VIEW_H - 0.5}
          stroke="currentColor"
          strokeWidth="1"
          className="text-subtle"
        />
      ) : (
        <>
          <path d={areaPath(values, max)} className="fill-accent-wash" />
          <path
            d={linePath(values, max)}
            fill="none"
            strokeWidth="1.5"
            vectorEffect="non-scaling-stroke"
            className="stroke-accent"
          />
          {showOverlay && (
            <path
              d={linePath(overlay, max)}
              fill="none"
              strokeWidth="1.5"
              strokeDasharray="3 2"
              vectorEffect="non-scaling-stroke"
              className="stroke-danger"
            />
          )}
        </>
      )}
    </svg>
  );
}

const VIEW_W = 100;
const VIEW_H = 32;

/** x for bucket `index`. A single bucket sits in the middle rather than at 0 —
 *  a one-point series has no width to spread over. */
function pointX(index: number, count: number): number {
  return count === 1 ? VIEW_W / 2 : (index * VIEW_W) / (count - 1);
}

function pointY(value: number, max: number): number {
  // One unit of headroom so a bucket at the window's own maximum is still
  // visibly a line rather than being clipped by the top edge.
  return VIEW_H - 1 - (value / max) * (VIEW_H - 2);
}

/** Coordinates are fixed to two decimals so the same array always produces the
 *  same `d` string — a chart whose markup jitters with float noise cannot be
 *  asserted on, and would also defeat React's own diffing. */
function coords(values: number[], max: number): string[] {
  return values.map(
    (value, index) => `${pointX(index, values.length).toFixed(2)} ${pointY(value, max).toFixed(2)}`,
  );
}

export function linePath(values: number[], max: number): string {
  if (values.length === 0) return "";
  return `M${coords(values, max).join(" L")}`;
}

export function areaPath(values: number[], max: number): string {
  if (values.length === 0) return "";
  const points = coords(values, max);
  const first = pointX(0, values.length).toFixed(2);
  const last = pointX(values.length - 1, values.length).toFixed(2);
  return `M${first} ${VIEW_H} L${points.join(" L")} L${last} ${VIEW_H} Z`;
}
