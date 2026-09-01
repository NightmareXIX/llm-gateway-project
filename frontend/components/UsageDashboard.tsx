"use client";

import { useState, type ReactNode } from "react";
import Link from "next/link";

import { cn } from "@/lib/cn";
import { absoluteTime, formatTokens, relativeTime } from "@/lib/format";
import { useQuotaOverview, useRecentRequests, useUsage } from "@/lib/hooks";
import { modelLabel, providerLabel, slotLabel } from "@/lib/models";
import type {
  CandidateStatus,
  ModelsResponse,
  ProviderSlice,
  RequestRow,
  UsageOverview,
  UsageWindow,
  WindowStatus,
} from "@/lib/types";
import { BarRow } from "./charts/BarRow";
import { Meter, formatPercent } from "./charts/Meter";
import { Sparkline } from "./charts/Sparkline";
import { EmptyState } from "./EmptyState";
import { ErrorState } from "./ErrorState";
import { Skeleton } from "./ui/Skeleton";

/**
 * "Here is *your* usage" — the dashboard, self-scoped (D44).
 *
 * Not an ops console, and deliberately not one (trap 17). There is no operator
 * identity in this system — `Principal` is frozen at four fields and `users`
 * has no role column — so every number on this page is the calling account's
 * own, scoped inside the SQL exactly like conversations and files. That is a
 * truthful product feature; an "all users" toggle would be a fake one.
 *
 * Panels in the order the questions get asked: how much did I send, how did it
 * go, who served it, what would it have cost, what is left, and what were the
 * last few calls.
 *
 * Two rules run through all of them:
 *
 * - **Every panel has an explicit empty state.** A brand-new account has no
 *   requests, and it is the first thing a reviewer sees. Every rate is computed
 *   through `Meter`, whose zero denominator renders an em dash rather than
 *   `NaN%`, and every bar's scale is guarded the same way.
 * - **The cost is labelled as a fiction.** It is computed at read time from a
 *   checked-in price table (D46), never billed and never stored — so the panel
 *   says so, in the same disclosure register this project has used for
 *   provenance since Phase 2. Unpriced models are counted beside it rather
 *   than folded into it as zero (trap 7).
 */
export function UsageDashboard() {
  const [usageWindow, setUsageWindow] = useState<UsageWindow>("24h");
  const { usage, error, isLoading } = useUsage(usageWindow);

  return (
    <div className="mx-auto w-full max-w-4xl px-4 py-8 sm:px-6">
      <header className="mb-6 flex flex-wrap items-end justify-between gap-3">
        <div>
          <h1 className="font-serif text-2xl text-ink">Usage</h1>
          <p className="mt-1 text-sm text-ink-secondary">
            Your own traffic through the gateway. Nobody else&apos;s.
          </p>
        </div>
        <div className="flex items-center gap-3">
          <WindowSwitch value={usageWindow} onChange={setUsageWindow} />
          <Link
            href="/chat"
            className="text-sm font-medium text-accent underline-offset-4 hover:underline"
          >
            Back to chat
          </Link>
        </div>
      </header>

      {error ? (
        <ErrorState error={error} />
      ) : isLoading || !usage ? (
        <DashboardSkeleton />
      ) : (
        <div className="space-y-6">
          <VolumePanel usage={usage} usageWindow={usageWindow} />
          <OutcomePanel usage={usage} />
          <ProviderPanel usage={usage} />
          <CostPanel usage={usage} />
          <QuotaPanel />
          <RecentRequestsPanel />
        </div>
      )}
    </div>
  );
}

const WINDOWS: { value: UsageWindow; label: string }[] = [
  { value: "1h", label: "Last hour" },
  { value: "24h", label: "24 hours" },
  { value: "7d", label: "7 days" },
];

const WINDOW_PHRASE: Record<UsageWindow, string> = {
  "1h": "last hour",
  "24h": "last 24 hours",
  "7d": "last 7 days",
};

/**
 * The three windows the server accepts, as a segmented control.
 *
 * The same shape the theme picker uses, for the same reason: three real
 * options whose current state should be readable without pressing anything.
 * Switching is a *key* change in `useUsage`, not a refetch of one entry.
 */
function WindowSwitch({
  value,
  onChange,
}: {
  value: UsageWindow;
  onChange: (next: UsageWindow) => void;
}) {
  return (
    <div
      role="radiogroup"
      aria-label="Time window"
      className="flex gap-1 rounded-control bg-sunken p-1"
    >
      {WINDOWS.map((option) => (
        <button
          key={option.value}
          type="button"
          role="radio"
          aria-checked={value === option.value}
          onClick={() => onChange(option.value)}
          className={cn(
            "h-7 rounded-[calc(var(--radius-control)-2px)] px-2.5 text-xs font-medium transition-colors",
            value === option.value
              ? "bg-raised text-ink shadow-sm"
              : "text-ink-secondary hover:text-ink",
          )}
        >
          {option.label}
        </button>
      ))}
    </div>
  );
}

/** A titled card. Every panel is one, so the page keeps one vertical rhythm. */
function Panel({
  title,
  note,
  children,
}: {
  title: string;
  note?: string;
  children: ReactNode;
}) {
  return (
    <section className="rounded-card border border-subtle bg-raised p-4">
      <div className="mb-3">
        <h2 className="text-sm font-medium text-ink">{title}</h2>
        {note && <p className="mt-0.5 text-xs text-ink-tertiary">{note}</p>}
      </div>
      {children}
    </section>
  );
}

function Stat({ label, value, hint }: { label: string; value: string; hint?: string }) {
  return (
    <div>
      <p className="text-xs text-ink-tertiary">{label}</p>
      <p className="text-lg font-medium text-ink tabular-nums">{value}</p>
      {hint && <p className="text-[0.6875rem] text-ink-tertiary">{hint}</p>}
    </div>
  );
}

// --------------------------------------------------------------------------- //
// 1. Volume over time
// --------------------------------------------------------------------------- //
function VolumePanel({
  usage,
  usageWindow,
}: {
  usage: UsageOverview;
  usageWindow: UsageWindow;
}) {
  const totals = usage.volume.map((point) => point.total);
  const errors = usage.volume.map((point) => point.errors);
  const requests = usage.outcomes.total;

  return (
    <Panel
      title="Requests over time"
      note={`${absoluteTime(usage.since)} — ${absoluteTime(usage.until)}, in local time`}
    >
      {requests === 0 ? (
        <EmptyState
          size="sm"
          title="Nothing yet"
          description={`No requests in the ${WINDOW_PHRASE[usageWindow]}. Send a message and it shows up here.`}
        />
      ) : (
        <>
          <Sparkline
            values={totals}
            overlay={errors}
            ariaLabel={`Requests per bucket over the ${WINDOW_PHRASE[usageWindow]}`}
          />
          <div className="mt-3 grid grid-cols-2 gap-3 sm:grid-cols-4">
            <Stat label="Requests" value={requests.toLocaleString()} />
            <Stat label="Succeeded" value={usage.outcomes.ok.toLocaleString()} />
            <Stat label="Failed" value={usage.outcomes.errors.toLocaleString()} />
            <Stat label="Replayed" value={usage.outcomes.replays.toLocaleString()} />
          </div>
        </>
      )}
    </Panel>
  );
}

// --------------------------------------------------------------------------- //
// 2. Outcome rates
// --------------------------------------------------------------------------- //
function OutcomePanel({ usage }: { usage: UsageOverview }) {
  const outcomes = usage.outcomes;

  return (
    <Panel
      title="Outcomes"
      note="Rates over every request in the window — a failure and a cache hit are both requests somebody made."
    >
      <div className="grid gap-3 sm:grid-cols-3">
        <Meter
          label="Error rate"
          value={outcomes.errors}
          total={outcomes.total}
          tone="danger"
          hint={`${outcomes.errors.toLocaleString()} of ${outcomes.total.toLocaleString()}`}
        />
        <Meter
          label="Cache hit rate"
          value={outcomes.cache_hits}
          total={outcomes.total}
          hint={`${outcomes.cache_hits.toLocaleString()} served without a provider call`}
        />
        <Meter
          label="Failover rate"
          value={outcomes.substituted}
          total={outcomes.total}
          tone="warn"
          hint={`${outcomes.substituted.toLocaleString()} substituted · ${outcomes.multi_attempt.toLocaleString()} took more than one attempt`}
        />
      </div>
      <div className="mt-3 grid grid-cols-2 gap-3 sm:grid-cols-3">
        <Stat label="Tokens in" value={formatTokens(outcomes.tokens_in)} />
        <Stat label="Tokens out" value={formatTokens(outcomes.tokens_out)} />
        <Stat
          label="Wasted output"
          value={formatTokens(outcomes.wasted_tokens_out)}
          hint="generated by attempts that were discarded"
        />
      </div>
    </Panel>
  );
}

// --------------------------------------------------------------------------- //
// 3. Provider distribution
// --------------------------------------------------------------------------- //
function ProviderPanel({ usage }: { usage: UsageOverview }) {
  const slices = usage.providers;
  const max = Math.max(0, ...slices.map((slice) => slice.requests));

  return (
    <Panel
      title="Who served it"
      note="Real upstream calls only — cache hits and requests that never reached a provider are excluded."
    >
      {slices.length === 0 ? (
        <EmptyState
          size="sm"
          title="No provider calls yet"
          description="Every request in this window was either served from cache or never reached a provider."
        />
      ) : (
        <ul className="space-y-3">
          {slices.map((slice) => (
            <li key={sliceKey(slice)}>
              <BarRow
                label={sliceLabel(slice)}
                value={slice.requests}
                max={max}
                valueLabel={`${slice.requests.toLocaleString()} · ${formatTokens(
                  slice.tokens_in + slice.tokens_out,
                )} tok`}
                sublabel={
                  slice.simulated_cost === null
                    ? "No price on file — not counted in the total below."
                    : `≈ ${formatCost(slice.simulated_cost, usage.currency)}`
                }
                tone={slice.simulated_cost === null ? "warn" : "accent"}
              />
            </li>
          ))}
        </ul>
      )}
    </Panel>
  );
}

function sliceKey(slice: ProviderSlice): string {
  return `${slice.provider}:${slice.model ?? ""}`;
}

function sliceLabel(slice: ProviderSlice): string {
  return `${providerLabel(slice.provider)} · ${slice.model ? modelLabel(slice.model) : "unknown model"}`;
}

// --------------------------------------------------------------------------- //
// 4. Simulated cost
// --------------------------------------------------------------------------- //
function CostPanel({ usage }: { usage: UsageOverview }) {
  const split = usage.pool_split;

  return (
    <Panel
      title="Simulated cost"
      // The disclosure, not a footnote: this number is a fiction computed at
      // read time from a checked-in price table. Nothing was billed and
      // nothing was stored, and saying so is the same register this project
      // has used for provenance since Phase 2.
      note="Computed now from a checked-in price table — nothing here was billed, and the free tiers this gateway runs on cost nothing in reality."
    >
      <div className="grid gap-3 sm:grid-cols-3">
        <Stat
          label="This window"
          value={formatCost(usage.total_cost, usage.currency)}
          hint={
            usage.unpriced_requests > 0
              ? `${usage.unpriced_requests.toLocaleString()} ${plural(usage.unpriced_requests, "request")} unpriced`
              : undefined
          }
        />
        <Stat
          label="Shared pool"
          value={formatCost(split.shared_cost, usage.currency)}
          hint={`${split.shared_requests.toLocaleString()} ${plural(split.shared_requests, "request")}`}
        />
        <Stat
          label="Your own keys"
          value={formatCost(split.private_cost, usage.currency)}
          hint={`${split.private_requests.toLocaleString()} ${plural(split.private_requests, "request")}`}
        />
      </div>
      {usage.unpriced_requests > 0 && (
        <p className="mt-3 text-xs text-warn">
          Some requests were served by a model with no entry in the price table. They are counted
          above but excluded from the total — an unpriced model is unpriced, not free.
        </p>
      )}
      <p className="mt-3 text-[0.6875rem] text-ink-tertiary">
        The shared/private split applies one blended rate across the window, so it is an
        approximation rather than a ledger.
      </p>
    </Panel>
  );
}

function plural(count: number, noun: string): string {
  return count === 1 ? noun : `${noun}s`;
}

/**
 * A `Decimal`-as-string from the wire, formatted for a human.
 *
 * The conversion to `Number` happens **here and only here**, at the last
 * possible moment: display rounding is lossy by definition, and confining it
 * to the edge is what keeps the exact value the server computed intact
 * everywhere else. `null` is "no price on file", which is a different
 * statement from zero and is printed as a different thing.
 */
export function formatCost(amount: string | null, currency: string | null): string {
  if (amount === null) return "—";
  const value = Number(amount);
  if (!Number.isFinite(value)) return "—";
  if (currency === null) return value.toFixed(4);

  try {
    return new Intl.NumberFormat(undefined, {
      style: "currency",
      currency,
      minimumFractionDigits: 2,
      maximumFractionDigits: 4,
    }).format(value);
  } catch {
    // An unrecognised currency code is the price table's problem, not a reason
    // for the panel to fail to render.
    return `${value.toFixed(4)} ${currency}`;
  }
}

// --------------------------------------------------------------------------- //
// 5. Quota utilization
// --------------------------------------------------------------------------- //
/**
 * What is left, under *the caller's own resolved scope* (D44).
 *
 * Read from `/v1/admin/quota`, which delegates to the very handler behind
 * `/v1/models` — a shared-pool user sees the shared pool's remainder, a
 * private-key user sees their own, and the two surfaces cannot disagree
 * because they are one computation. Breaker state is deliberately not
 * duplicated here: it is already disclosed per-candidate in the picker.
 */
function QuotaPanel() {
  const { quota, error, isLoading } = useQuotaOverview();

  return (
    <Panel
      title="What's left"
      note="Live remaining budget per candidate, under whichever pool serves you."
    >
      {error ? (
        <p className="text-sm text-danger">Could not load quota status.</p>
      ) : isLoading || !quota ? (
        <div className="space-y-3">
          <Skeleton className="h-8 w-full" />
          <Skeleton className="h-8 w-full" />
        </div>
      ) : (
        <QuotaRows quota={quota} />
      )}
    </Panel>
  );
}

function QuotaRows({ quota }: { quota: ModelsResponse }) {
  const rows = quotaRows(quota);

  if (rows.length === 0) {
    return (
      <EmptyState
        size="sm"
        title="No budget to report"
        description="The gateway isn't tracking a window for any candidate right now."
      />
    );
  }

  return (
    <ul className="space-y-3">
      {rows.map((row) => {
        const used = Math.max(0, row.window.limit - row.window.remaining);
        return (
          <li key={`${row.candidate.provider}:${row.candidate.model}:${row.window.window}`}>
            <BarRow
              label={`${providerLabel(row.candidate.provider)} · ${modelLabel(row.candidate.model)}`}
              value={used}
              max={row.window.limit}
              valueLabel={`${used.toLocaleString()} / ${row.window.limit.toLocaleString()} ${row.window.window}`}
              sublabel={`${slotLabel(row.slot)} · ${formatPercent(
                row.window.limit > 0 ? used / row.window.limit : 0,
              )} spent`}
              tone={row.candidate.status === "available" ? "accent" : "warn"}
            />
          </li>
        );
      })}
    </ul>
  );
}

type QuotaRow = { slot: string; candidate: CandidateStatus; window: WindowStatus };

/**
 * One row per candidate, on its most legible window.
 *
 * Two reductions, both deliberate. Candidates repeat across slots — `auto`
 * lists every candidate the fleet can reach — so they are de-duplicated on
 * `(provider, model)` and attributed to the first slot that offered them. And
 * a candidate can be tracked on four windows at once; the daily one is the one
 * a person can act on, so it wins, with the first tracked window as the
 * fallback for a candidate that has no `rpd`. A candidate with no tracked
 * window at all is skipped rather than drawn as an empty bar — there is
 * nothing to report, which is different from nothing left.
 */
export function quotaRows(quota: ModelsResponse): QuotaRow[] {
  const seen = new Set<string>();
  const rows: QuotaRow[] = [];

  for (const entry of quota.data) {
    for (const candidate of entry.candidates) {
      const key = `${candidate.provider}:${candidate.model}`;
      if (seen.has(key)) continue;
      const window = candidate.windows.find((each) => each.window === "rpd") ?? candidate.windows[0];
      if (!window) continue;
      seen.add(key);
      rows.push({ slot: entry.id, candidate, window });
    }
  }

  return rows;
}

// --------------------------------------------------------------------------- //
// 6. Recent calls
// --------------------------------------------------------------------------- //
function RecentRequestsPanel() {
  const { requests, error, isLoading } = useRecentRequests();

  return (
    <Panel title="Recent calls" note="Your most recent requests, newest first.">
      {error ? (
        <p className="text-sm text-danger">Could not load your recent requests.</p>
      ) : isLoading || !requests ? (
        <div className="space-y-2">
          <Skeleton className="h-6 w-full" />
          <Skeleton className="h-6 w-full" />
          <Skeleton className="h-6 w-full" />
        </div>
      ) : requests.length === 0 ? (
        <EmptyState
          size="sm"
          title="No calls yet"
          description="Requests appear here as soon as you send one."
        />
      ) : (
        <div className="overflow-x-auto">
          <table className="w-full min-w-[38rem] text-left text-xs">
            <thead className="text-ink-tertiary">
              <tr>
                {COLUMNS.map((column) => (
                  <th key={column} scope="col" className="py-1 pr-3 font-normal">
                    {column}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody className="text-ink-secondary">
              {requests.map((row) => (
                <RequestRowCells key={row.id} row={row} />
              ))}
            </tbody>
          </table>
        </div>
      )}
    </Panel>
  );
}

const COLUMNS = ["When", "Slot", "Served by", "Status", "Tokens", "Latency"];

function RequestRowCells({ row }: { row: RequestRow }) {
  return (
    <tr className="border-t border-subtle">
      <td className="whitespace-nowrap py-1.5 pr-3" title={absoluteTime(row.created_at)}>
        {relativeTime(row.created_at)}
      </td>
      <td className="py-1.5 pr-3 font-mono">{row.requested_slot ?? "—"}</td>
      <td className="py-1.5 pr-3">{servedByLabel(row)}</td>
      <td className="py-1.5 pr-3">
        <StatusPill row={row} />
      </td>
      <td className="whitespace-nowrap py-1.5 pr-3 font-mono tabular-nums">
        {row.tokens_in === null && row.tokens_out === null
          ? "—"
          : `${formatTokens(row.tokens_in ?? 0)} / ${formatTokens(row.tokens_out ?? 0)}`}
      </td>
      <td className="whitespace-nowrap py-1.5 font-mono tabular-nums">
        {row.latency_ms === null ? "—" : `${row.latency_ms.toLocaleString()} ms`}
      </td>
    </tr>
  );
}

/**
 * A cache hit is labelled as one, and a NULL provider is an em dash.
 *
 * Both are the table's half of the rules the aggregates follow: a cache hit's
 * row names the candidate that *originally* answered (trap 5), so printing it
 * as an ordinary provider call would claim a request that never went out, and
 * a NULL provider means "never got that far" (trap 6) rather than a provider
 * called "unknown".
 */
function servedByLabel(row: RequestRow): string {
  if (row.cache_hit) return "from cache";
  if (!row.provider) return "—";
  return `${providerLabel(row.provider)} · ${row.model ? modelLabel(row.model) : "—"}`;
}

/**
 * `ok` / `replayed` / everything else — the same three-way split the
 * aggregates use, so this table and the error rate above it cannot disagree
 * about what counts as a failure. An idempotent replay is a success that cost
 * nothing, not an error (trap 18).
 */
function StatusPill({ row }: { row: RequestRow }) {
  const failed = row.status !== "ok" && row.status !== "replayed";
  return (
    <span
      className={cn(
        "rounded-full px-1.5 py-0.5 font-medium",
        failed ? "bg-danger-wash text-danger" : "bg-sunken text-ink-secondary",
      )}
      title={row.error_code ?? undefined}
    >
      {failed && row.error_code ? row.error_code : row.status}
      {row.substituted && !failed ? " · substituted" : ""}
    </span>
  );
}

function DashboardSkeleton() {
  return (
    <div className="space-y-6" aria-busy>
      <Skeleton className="h-40 w-full rounded-card" />
      <Skeleton className="h-32 w-full rounded-card" />
      <Skeleton className="h-32 w-full rounded-card" />
    </div>
  );
}
