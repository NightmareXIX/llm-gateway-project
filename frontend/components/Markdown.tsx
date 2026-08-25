"use client";

import { memo } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";

import { cn } from "@/lib/cn";

/**
 * A model answer, rendered as the markdown it actually is.
 *
 * Every provider in the fleet answers in markdown — headings, fenced code,
 * GFM tables, bullet lists — and Phase 1 rendered that straight into a `<p>`
 * under `white-space: pre-wrap`, which is why a table arrived as a column of
 * pipes and `**bold**` arrived with its asterisks. Nothing upstream changed
 * here: the canonical `text` block still stores exactly what the model sent,
 * and this component is only the last step of the pipeline finally reading it.
 *
 * Raw HTML is deliberately *not* enabled. `react-markdown` ignores embedded
 * HTML unless `rehype-raw` is added, and it is not added on purpose: the text
 * being rendered comes from a third-party provider, over an API a user's own
 * prompt can steer, so a `<script>` or an `onerror=` in a model's answer must
 * stay inert text rather than become DOM. GFM's autolinks are safe (the link
 * is escaped and rendered as an anchor), so links open in a new tab with
 * `noopener` rather than being followed in place.
 *
 * Memoized on the text, because the streaming path re-renders this on every
 * delta and reparsing an answer mid-stream is the one place the cost is real.
 */
export const Markdown = memo(function Markdown({
  text,
  className,
}: {
  text: string;
  className?: string;
}) {
  return (
    <div className={cn("markdown-answer", className)}>
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        components={{
          a: ({ href, children, ...props }) => (
            <a href={href} target="_blank" rel="noopener noreferrer nofollow" {...props}>
              {children}
            </a>
          ),
          // A wide table must scroll inside its own turn rather than widening
          // the transcript column and pushing every other message sideways.
          table: ({ children, ...props }) => (
            <div className="markdown-scroll">
              <table {...props}>{children}</table>
            </div>
          ),
        }}
      >
        {text}
      </ReactMarkdown>
    </div>
  );
});
