import { cn } from "@/lib/cn";

/**
 * Decorative by default — `aria-hidden`, because a spinner next to a label the
 * user can already read adds nothing, and the surrounding control carries
 * `aria-busy`. Pass a `label` only when the spinner is the *only* indication
 * that something is happening.
 */
export function Spinner({ className, label }: { className?: string; label?: string }) {
  return (
    <span
      role={label ? "status" : undefined}
      aria-hidden={label ? undefined : true}
      aria-label={label}
      className={cn("inline-block size-4 shrink-0", className)}
    >
      <svg viewBox="0 0 24 24" fill="none" className="size-full animate-spin">
        <circle cx="12" cy="12" r="9" stroke="currentColor" strokeWidth="2.5" opacity="0.25" />
        <path
          d="M21 12a9 9 0 0 0-9-9"
          stroke="currentColor"
          strokeWidth="2.5"
          strokeLinecap="round"
        />
      </svg>
    </span>
  );
}
