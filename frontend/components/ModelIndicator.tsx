"use client";

import { cn } from "@/lib/cn";
import { formatTokens } from "@/lib/format";
import { modelLabel, providerLabel } from "@/lib/models";
import type { Provenance } from "@/lib/provenance";
import { Tooltip } from "./ui/Tooltip";
import { VisuallyHidden } from "./ui/VisuallyHidden";

/**
 * The D1/D2 disclosure chip. Rendered under every assistant message, always.
 *
 * This component's contract is frozen. Its four rules come from §1.1's
 * "Frontend indicator" paragraph, and all four are implemented now even though
 * Phase 1 can only ever exercise the first — the gateway has one provider, no
 * failover and no perception lane, so `substituted` is always false, `attempts`
 * is always 1 and `degraded` is always false. They ship anyway, because the
 * point of building this in Phase 1 is that Phase 2 turns them on by returning
 * different data, not by editing this file.
 *
 *   1. Always render `served_by`. There is no state in which the gateway
 *      declines to say who answered. Not collapsible, not behind a toggle.
 *   2. `substituted` → render the mismatch, not just the substitute. "Something
 *      else answered" is the disclosure; hiding which slot was asked for would
 *      make the substitution silent again.
 *   3. `attempts > 1` → a subtle marker carrying the attempt trail.
 *   4. `degraded` → say so plainly.
 *
 * Everything it needs arrives as one `Provenance` object, built by an adapter in
 * `lib/provenance.ts`. That indirection is what lets a stored message, a
 * completion response and (in Phase 2) a `done` event all render through the
 * same component.
 */
export function ModelIndicator({
  provenance,
  className,
}: {
  provenance: Provenance;
  className?: string;
}) {
  const { servedBy, requestedSlot, substituted, attempts, degraded, tokensIn, tokensOut } =
    provenance;

  const model = modelLabel(servedBy.model);
  const provider = providerLabel(servedBy.provider);

  return (
    <div
      className={cn(
        "flex flex-wrap items-center gap-x-2 gap-y-1 text-xs text-ink-tertiary",
        className,
      )}
    >
      {/* Rule 1 — who answered. */}
      <span className="inline-flex items-center gap-1.5">
        <span
          aria-hidden
          className={cn(
            "size-1.5 rounded-full",
            substituted ? "bg-warn" : degraded ? "bg-warn" : "bg-accent",
          )}
        />
        <span className="font-medium text-ink-secondary">{model}</span>
        <span aria-hidden>·</span>
        <span>{provider}</span>
        {servedBy.slot && (
          <span className="font-mono text-[0.6875rem] text-ink-tertiary">
            <VisuallyHidden>served by slot </VisuallyHidden>
            {servedBy.slot}
          </span>
        )}
      </span>

      {/* Rule 2 — the mismatch, when an explicit choice was overridden. */}
      {substituted && requestedSlot && (
        <>
          <Separator />
          <span className="font-medium text-warn">{requestedSlot} was unavailable</span>
        </>
      )}

      {/* Rule 3 — the attempt trail. */}
      {attempts > 1 && (
        <>
          <Separator />
          <Tooltip content={<AttemptTrail provenance={provenance} />}>
            <span
              tabIndex={0}
              className="cursor-help rounded border-b border-dotted border-ink-tertiary text-ink-tertiary"
            >
              {attempts} attempts
            </span>
          </Tooltip>
          {/* The trail is information, not decoration, so it is also available
              without a pointer. */}
          <VisuallyHidden>
            <AttemptTrail provenance={provenance} />
          </VisuallyHidden>
        </>
      )}

      {/* Rule 4 — degraded perception. */}
      {degraded && (
        <>
          <Separator />
          <span className="font-medium text-warn">read with local extraction</span>
        </>
      )}

      {(tokensIn !== null || tokensOut !== null) && (
        <>
          <Separator />
          <span className="font-mono text-[0.6875rem]">
            <VisuallyHidden>tokens: </VisuallyHidden>
            {tokensIn !== null && <>{formatTokens(tokensIn)} in</>}
            {tokensIn !== null && tokensOut !== null && " · "}
            {tokensOut !== null && <>{formatTokens(tokensOut)} out</>}
          </span>
        </>
      )}
    </div>
  );
}

function Separator() {
  return (
    <span aria-hidden className="text-strong">
      |
    </span>
  );
}

/**
 * The per-attempt detail behind rule 3.
 *
 * Phase 2 populates `attemptTrail`; until then the count is all there is, and
 * the fallback says exactly that rather than inventing a history. `wasted`
 * tokens are surfaced because they are the honest cost of a restart — the
 * gateway really spent them, and D1 records them for exactly this reason.
 */
function AttemptTrail({ provenance }: { provenance: Provenance }) {
  const { attemptTrail, attempts, servedBy, wastedTokensOut } = provenance;

  if (!attemptTrail?.length) {
    return (
      <span>
        {attempts} attempts — answered by {modelLabel(servedBy.model)}
        {wastedTokensOut > 0 && `, ${formatTokens(wastedTokensOut)} tokens discarded`}.
      </span>
    );
  }

  return (
    <ol className="space-y-0.5">
      {attemptTrail.map((attempt) => (
        <li key={attempt.attempt}>
          <span className="font-mono">{attempt.attempt}.</span> {modelLabel(attempt.model)} ·{" "}
          {providerLabel(attempt.provider)} —{" "}
          {attempt.outcome === "served" ? "answered" : (attempt.reason ?? "failed")}
        </li>
      ))}
    </ol>
  );
}
