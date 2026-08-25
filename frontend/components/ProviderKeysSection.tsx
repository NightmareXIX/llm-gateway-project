"use client";

import { useState } from "react";

import { GatewayError } from "@/lib/api";
import { cn } from "@/lib/cn";
import { useProviderKeys } from "@/lib/hooks";
import { providerLabel } from "@/lib/models";
import type { ProviderKeyStatus } from "@/lib/types";
import { formatWaitSeconds } from "./ErrorState";
import { Button } from "./ui/Button";
import { Skeleton } from "./ui/Skeleton";

/**
 * Bring your own key — the settings surface for §9.2, §9.6 and §9.8.
 *
 * **One row per enabled provider, whether or not there is a key for it.** The
 * gateway returns the full list from `GET /v1/provider-keys` precisely so this
 * component never has to know which providers exist; an account with no keys
 * still gets three rows reading *Using shared pool*, which is what makes the
 * feature discoverable rather than something you have to already know about.
 *
 * Three pieces of behaviour that are the point rather than polish:
 *
 * - **The input is cleared on failure as well as on success.** A rejected key
 *   left sitting in the box invites a second submit of the same bad string,
 *   against a route that allows five attempts an hour (D43) — and it leaves a
 *   live credential in the DOM for as long as the dialog is open.
 * - **The 422 and the 503 are different sentences.** "Gemini rejected this key"
 *   and "we could not reach Gemini to check" lead to opposite next actions, and
 *   §9.2 exists because collapsing them is exactly the confusion this feature
 *   causes when it goes wrong. Both render inline, under the row they belong
 *   to, and stay there until the next attempt — not as a toast that vanishes
 *   before it has been read.
 * - **A stored key the gateway later found broken says so** (D40). A live
 *   `AuthFailed` flips the row's `validation_status` to `invalid`, and this is
 *   where that write pays off: the row warns, and offers a re-check rather than
 *   only a removal, because a key that was rejected during a provider outage is
 *   worth asking about twice.
 */
export function ProviderKeysSection() {
  const { rows, error, isLoading, add, remove, revalidate } = useProviderKeys();

  return (
    <section>
      <h3 className="mb-2 text-sm font-medium text-ink">Provider API keys</h3>

      {/* §9.8's disclosure, in plain language and above the rows rather than
          behind a tooltip: this is the term a person is agreeing to by pasting
          a credential, and it belongs where they can read it before they do.
          Same reasoning as the composer's third-party-extraction notice. */}
      <p className="mb-3 text-xs leading-relaxed text-ink-tertiary">
        Add your own key and the gateway will use it for that provider instead of its shared
        pool. Requests made with your key are sent to that provider under your account, billed
        to you, and governed by their terms rather than ours. Keys are encrypted at rest, never
        shown again after you add them, and removing one takes effect on your very next message.
      </p>

      {isLoading ? (
        <div className="space-y-2">
          <Skeleton className="h-14 w-full" />
          <Skeleton className="h-14 w-full" />
        </div>
      ) : error ? (
        <p className="text-sm text-danger">Could not load your provider keys.</p>
      ) : (
        <ul className="space-y-2">
          {(rows ?? []).map((row) => (
            <li key={row.provider}>
              <ProviderKeyRow row={row} add={add} remove={remove} revalidate={revalidate} />
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}

/** One provider: its current pool, and the actions available in that state. */
function ProviderKeyRow({
  row,
  add,
  remove,
  revalidate,
}: {
  row: ProviderKeyStatus;
  add: (provider: string, key: string) => Promise<void>;
  remove: (provider: string) => Promise<void>;
  revalidate: (provider: string) => Promise<void>;
}) {
  const [adding, setAdding] = useState(false);
  const [value, setValue] = useState("");
  // Which write is in flight, so the spinner lands on the button that was
  // pressed rather than on every button in the row.
  const [busy, setBusy] = useState<"add" | "remove" | "check" | null>(null);
  const [failure, setFailure] = useState<unknown>(null);

  const name = providerLabel(row.provider);
  const stored = row.key;
  const rejected = stored?.validation_status === "invalid";

  /** Every write shares this: clear the box either way, keep the reason. */
  async function run(kind: "add" | "remove" | "check", action: () => Promise<void>) {
    setBusy(kind);
    setFailure(null);
    try {
      await action();
      setValue("");
      setAdding(false);
    } catch (error) {
      setValue("");
      setFailure(error);
    } finally {
      setBusy(null);
    }
  }

  return (
    <div
      // Named as a group so each row's controls are addressable — three rows
      // carrying a button labelled "Remove" is otherwise ambiguous to a screen
      // reader for the same reason it is ambiguous to a test.
      role="group"
      aria-label={name}
      className={cn(
        "rounded-control border bg-raised p-3",
        rejected ? "border-warn" : "border-strong",
      )}
    >
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="min-w-0">
          <p className="text-sm font-medium text-ink">{name}</p>
          <p className="text-xs text-ink-tertiary">
            {stored ? (
              <>
                Using your key ·{" "}
                <span className="font-mono text-ink-secondary">{stored.masked}</span>
              </>
            ) : (
              "Using shared pool"
            )}
          </p>
        </div>

        <div className="flex items-center gap-2">
          {rejected && (
            <Button
              size="sm"
              variant="secondary"
              loading={busy === "check"}
              onClick={() => void run("check", () => revalidate(row.provider))}
            >
              Check again
            </Button>
          )}
          {stored ? (
            <Button
              size="sm"
              variant="ghost"
              loading={busy === "remove"}
              onClick={() => void run("remove", () => remove(row.provider))}
            >
              Remove
            </Button>
          ) : adding ? (
            <Button
              size="sm"
              variant="ghost"
              disabled={busy !== null}
              onClick={() => setAdding(false)}
            >
              Cancel
            </Button>
          ) : (
            <Button size="sm" variant="secondary" onClick={() => setAdding(true)}>
              Add key
            </Button>
          )}
        </div>
      </div>

      {/* D40's disclosure, said in the row rather than only in a status field. */}
      {rejected && (
        <p className="mt-2 text-xs font-medium text-warn">
          {name} rejected this key the last time it was used, so requests are going to the
          shared pool. Check it again, or remove it and add a new one.
        </p>
      )}

      {!stored && adding && (
        <form
          className="mt-3 flex flex-wrap items-end gap-2"
          onSubmit={(event) => {
            event.preventDefault();
            if (value.trim()) void run("add", () => add(row.provider, value.trim()));
          }}
        >
          <label className="min-w-0 flex-1">
            <span className="mb-1 block text-xs text-ink-tertiary">{name} API key</span>
            <input
              // `password`, not `text`: this is a live credential, and it is
              // typed into a dialog that is routinely open on a shared screen.
              type="password"
              autoComplete="off"
              spellCheck={false}
              // Never pre-filled from anywhere. There is no endpoint that can
              // return a stored key's plaintext, and there should not be one.
              value={value}
              disabled={busy !== null}
              onChange={(event) => setValue(event.target.value)}
              placeholder={`Paste your ${name} key`}
              className={cn(
                "h-9 w-full rounded-control border border-strong bg-ground px-3 font-mono",
                "text-sm text-ink placeholder:font-sans placeholder:text-ink-tertiary",
                "disabled:cursor-not-allowed disabled:opacity-60",
              )}
            />
          </label>
          <Button
            type="submit"
            size="sm"
            variant="primary"
            loading={busy === "add"}
            loadingLabel="Checking…"
            disabled={!value.trim()}
          >
            Save
          </Button>
        </form>
      )}

      {failure !== null && <KeyFailure error={failure} provider={name} />}
    </div>
  );
}

/**
 * A failed write, in the words that failure deserves.
 *
 * Branching on `code` before `status`, the rule `ErrorState` already follows:
 * the two 4xx-shaped outcomes on this route mean opposite things, and the 429
 * is not a failure at all.
 */
function KeyFailure({ error, provider }: { error: unknown; provider: string }) {
  const described = describeKeyFailure(error, provider);
  return (
    <p
      role="alert"
      className={cn(
        "mt-2 text-xs font-medium",
        described.tone === "wait" ? "text-warn" : "text-danger",
      )}
    >
      {described.message}
    </p>
  );
}

export function describeKeyFailure(
  error: unknown,
  provider: string,
): { message: string; tone: "error" | "wait" } {
  if (error instanceof GatewayError) {
    // D43's anti-abuse floor. A wait, not a rejection — and the gateway says
    // how long, so `ErrorState`'s own rounding is reused rather than re-guessed.
    if (error.isRateLimited) {
      return {
        tone: "wait",
        message:
          error.retryAfterS !== null
            ? `Too many key checks. Try again in about ${formatWaitSeconds(error.retryAfterS)}.`
            : "Too many key checks in the last hour. Try again shortly.",
      };
    }
    // The provider itself said no. Its wording, verbatim — the gateway built
    // that sentence out of what the provider returned precisely so it could be
    // shown to a human, and nothing was stored.
    if (error.code === "invalid_provider_key") {
      return { tone: "error", message: `${error.message} Nothing was saved.` };
    }
    // We could not check. Explicitly *not* the sentence above (§9.2, trap 6).
    if (error.code === "provider_unavailable") {
      return {
        tone: "error",
        message: `Could not reach ${provider} to check this key. Nothing was saved — try again shortly.`,
      };
    }
    return { tone: "error", message: error.message };
  }

  return { tone: "error", message: "Could not reach the gateway. Try again in a moment." };
}
