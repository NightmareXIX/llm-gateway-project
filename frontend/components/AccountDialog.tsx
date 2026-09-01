"use client";

import { useState } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";

import { cn } from "@/lib/cn";
import { useMe } from "@/lib/hooks";
import { getSupabaseBrowserClient } from "@/lib/supabase/client";
import { THEMES, THEME_LABELS, useTheme, type Theme } from "@/lib/theme";
import { ProviderKeysSection } from "./ProviderKeysSection";
import { Button } from "./ui/Button";
import { Dialog } from "./ui/Dialog";
import { Skeleton } from "./ui/Skeleton";

/**
 * Account and appearance, in one centred modal.
 *
 * Previously these were two controls wedged into the sidebar — an identity line
 * competing for width with a Sign out button, and a theme cycler orphaned up in
 * the header. Both problems were the same problem: settings were being laid out
 * *around* the conversation list instead of being given their own surface.
 *
 * A modal fixes the crowding and the discoverability at once. It is still not a
 * settings *page* — model preferences belong to a later phase — but Phase 6
 * gave it the third thing an account genuinely owns: the provider keys that
 * decide whose credential answers your messages (§9.2), and Phase 7 added the
 * one link to what all of that has actually spent. It now holds who you are,
 * whose key pays, what it cost, how it looks, and the way out.
 */
export function AccountDialog({ open, onClose }: { open: boolean; onClose: () => void }) {
  const { me, isLoading } = useMe();
  const router = useRouter();
  const [signingOut, setSigningOut] = useState(false);

  return (
    <Dialog
      open={open}
      onClose={onClose}
      title="Account"
      footer={
        <>
          <Button variant="ghost" onClick={onClose} disabled={signingOut}>
            Close
          </Button>
          <Button
            variant="secondary"
            loading={signingOut}
            loadingLabel="Signing out…"
            onClick={async () => {
              setSigningOut(true);
              await getSupabaseBrowserClient().auth.signOut();
              router.replace("/login");
              router.refresh();
            }}
          >
            Sign out
          </Button>
        </>
      }
    >
      <div className="space-y-6">
        <section>
          {isLoading ? (
            <div className="space-y-2">
              <Skeleton className="h-4 w-48" />
              <Skeleton className="h-3 w-24" />
            </div>
          ) : (
            <dl className="space-y-2 text-sm">
              <div className="flex items-baseline justify-between gap-4">
                <dt className="text-ink-tertiary">Signed in as</dt>
                <dd className="min-w-0 truncate font-medium text-ink" title={me?.email}>
                  {me?.email ?? "—"}
                </dd>
              </div>
              <div className="flex items-baseline justify-between gap-4">
                <dt className="text-ink-tertiary">Plan</dt>
                <dd className="font-mono text-xs text-ink-secondary">{me?.tier ?? "—"}</dd>
              </div>
            </dl>
          )}
        </section>

        <section>
          <h3 className="mb-2 text-sm font-medium text-ink">Usage</h3>
          <p className="mb-2 text-xs leading-relaxed text-ink-tertiary">
            Request volume, which provider served what, how often the gateway had to fail over,
            and what it would have cost at list prices.
          </p>
          {/* Closes on the way out: the dialog is rendered by the chat shell,
              and leaving it mounted over a route it no longer belongs to is
              how a modal ends up hovering above the page you navigated to. */}
          <Link
            href="/usage"
            onClick={onClose}
            className="text-sm font-medium text-accent underline-offset-4 hover:underline"
          >
            View your usage
          </Link>
        </section>

        <ProviderKeysSection />

        <section>
          <h3 className="mb-2 text-sm font-medium text-ink">Appearance</h3>
          <ThemeSelect />
        </section>
      </div>
    </Dialog>
  );
}

/**
 * Light / dark / system as a segmented control.
 *
 * Three visible options rather than a cycling icon: a control whose current
 * state you have to press to discover is a poor fit for a preference you set
 * once. "System" is a real answer and stays selectable — a two-way switch
 * silently opts a user out of following their OS the first time they touch it.
 */
function ThemeSelect() {
  const { theme, mounted, select } = useTheme();

  if (!mounted) return <Skeleton className="h-9 w-full" />;

  return (
    <div
      role="radiogroup"
      aria-label="Colour theme"
      className="grid grid-cols-3 gap-1 rounded-control bg-sunken p-1"
    >
      {THEMES.map((value: Theme) => (
        <button
          key={value}
          type="button"
          role="radio"
          aria-checked={theme === value}
          onClick={() => select(value)}
          className={cn(
            "h-8 rounded-[calc(var(--radius-control)-2px)] text-sm font-medium transition-colors",
            theme === value ? "bg-raised text-ink shadow-sm" : "text-ink-secondary hover:text-ink",
          )}
        >
          {THEME_LABELS[value]}
        </button>
      ))}
    </div>
  );
}
