import type { ReactNode } from "react";
import { cn } from "@/lib/cn";

/**
 * The shared shape for "there is nothing here yet".
 *
 * Deliberately a component rather than three bespoke blocks: an empty sidebar,
 * an empty conversation and an empty search would otherwise each drift into
 * their own vertical rhythm, and inconsistent emptiness is the fastest way for
 * an app to feel unfinished.
 */
export function EmptyState({
  title,
  description,
  action,
  size = "md",
  className,
}: {
  title: ReactNode;
  description?: ReactNode;
  action?: ReactNode;
  size?: "sm" | "md" | "lg";
  className?: string;
}) {
  return (
    <div
      className={cn(
        "flex flex-col items-center justify-center text-center",
        size === "sm" ? "gap-2 px-4 py-8" : "gap-3 px-6 py-12",
        className,
      )}
    >
      <h2
        className={cn(
          "font-serif text-ink",
          size === "lg" ? "text-3xl sm:text-4xl" : size === "md" ? "text-xl" : "text-base",
        )}
      >
        {title}
      </h2>
      {description && (
        <p className={cn("max-w-sm text-ink-secondary", size === "lg" ? "text-base" : "text-sm")}>
          {description}
        </p>
      )}
      {action && <div className="mt-2">{action}</div>}
    </div>
  );
}
