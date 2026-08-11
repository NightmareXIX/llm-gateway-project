"use client";

import { useEffect, useState } from "react";

/**
 * A click-to-copy request id.
 *
 * The gateway puts `request_id` on every error specifically so a user report is
 * traceable to a log line. That only pays off if the id is easy to hand over —
 * a 26-character ULID transcribed by hand is a typo waiting to happen.
 */
export function RequestId({ value }: { value: string }) {
  const [copied, setCopied] = useState(false);

  useEffect(() => {
    if (!copied) return;
    const timer = setTimeout(() => setCopied(false), 1600);
    return () => clearTimeout(timer);
  }, [copied]);

  return (
    <button
      type="button"
      onClick={async () => {
        try {
          await navigator.clipboard.writeText(value);
          setCopied(true);
        } catch {
          // Clipboard blocked (insecure origin, denied permission). The id is
          // still on screen and selectable — nothing to report.
        }
      }}
      className="rounded px-1 py-0.5 font-mono text-xs text-ink-tertiary underline decoration-dotted underline-offset-2 transition-colors hover:bg-sunken hover:text-ink"
    >
      {copied ? "copied" : value}
    </button>
  );
}
