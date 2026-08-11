import { cn } from "@/lib/cn";

/**
 * A loading placeholder shaped like the content it stands in for.
 *
 * `aria-hidden` throughout: the surrounding region announces "loading" once,
 * and a screen reader reading out nine anonymous placeholder bars is noise.
 */
export function Skeleton({ className }: { className?: string }) {
  return (
    <span
      aria-hidden
      className={cn(
        "block rounded bg-sunken",
        "bg-[linear-gradient(90deg,var(--sunken),var(--border-subtle),var(--sunken))]",
        "bg-[length:200%_100%] [animation:shimmer_1.6s_ease-in-out_infinite]",
        className,
      )}
    />
  );
}
