"use client";

import { useEffect, useState, type FormEvent } from "react";
import { useRouter, useSearchParams } from "next/navigation";
import type { AuthError } from "@supabase/supabase-js";

import { getSupabaseBrowserClient } from "@/lib/supabase/client";
import { cn } from "@/lib/cn";
import { Button } from "./ui/Button";
import { TextField } from "./ui/TextField";

type Mode = "signin" | "signup";

const MIN_PASSWORD_LENGTH = 8;

/**
 * Sign in and sign up, in one card.
 *
 * The states this has to render are the reason it is not four lines of Supabase
 * UI: an invalid password, an unconfirmed email, an address that already has an
 * account, Supabase's own rate limit, and — the one most login forms forget —
 * the successful sign-up, which is *not* a redirect. The gateway runs with
 * `REQUIRE_VERIFIED_EMAIL=true`, so a new account has no usable session until
 * the confirmation link is clicked. Redirecting to /chat there would hand the
 * user a wall of 401s and no explanation.
 */
export function LoginForm({ demoAvailable = false }: { demoAvailable?: boolean }) {
  const router = useRouter();
  const params = useSearchParams();
  const next = params.get("next") ?? "/chat";

  const [mode, setMode] = useState<Mode>("signin");
  const [demoLoading, setDemoLoading] = useState(false);
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [fieldErrors, setFieldErrors] = useState<{ email?: string; password?: string }>({});
  const [formError, setFormError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const [confirmationSentTo, setConfirmationSentTo] = useState<string | null>(null);

  // A bounced confirmation link arrives here with a reason. Surfacing it beats
  // dropping the user on a form with no idea why the link did not work.
  useEffect(() => {
    const reason = params.get("reason");
    if (reason === "link_expired") {
      setFormError("That confirmation link has expired or was already used. Sign in below.");
    } else if (reason === "missing_code") {
      setFormError("That link was incomplete. Sign in below, or request a new one.");
    }
  }, [params]);

  function validate(): boolean {
    const errors: { email?: string; password?: string } = {};
    if (!email.trim()) errors.email = "Enter your email address.";
    else if (!/^\S+@\S+\.\S+$/.test(email.trim())) errors.email = "That doesn't look like an email address.";

    if (!password) errors.password = "Enter your password.";
    else if (mode === "signup" && password.length < MIN_PASSWORD_LENGTH) {
      errors.password = `Use at least ${MIN_PASSWORD_LENGTH} characters.`;
    }

    setFieldErrors(errors);
    return Object.keys(errors).length === 0;
  }

  async function onSubmit(event: FormEvent) {
    event.preventDefault();
    setFormError(null);
    if (!validate()) return;

    setSubmitting(true);
    const supabase = getSupabaseBrowserClient();

    try {
      if (mode === "signup") {
        const { data, error } = await supabase.auth.signUp({
          email: email.trim(),
          password,
          options: {
            emailRedirectTo: `${window.location.origin}/auth/callback?next=${encodeURIComponent(next)}`,
          },
        });
        if (error) {
          setFormError(describeAuthError(error, mode));
          return;
        }
        // Supabase returns a user with no identities when the address already
        // has an account — it declines to confirm or deny by design. Both paths
        // land on the same panel, which is the correct behaviour anyway.
        if (data.session) {
          router.replace(next);
          router.refresh();
        } else {
          setConfirmationSentTo(email.trim());
        }
        return;
      }

      const { error } = await supabase.auth.signInWithPassword({
        email: email.trim(),
        password,
      });
      if (error) {
        if (isUnconfirmedEmail(error)) {
          setConfirmationSentTo(email.trim());
          return;
        }
        setFormError(describeAuthError(error, mode));
        return;
      }

      router.replace(next);
      // The session cookie was just written; refresh so the server components
      // above this route see it on the next render.
      router.refresh();
    } catch {
      setFormError("Couldn't reach the authentication service. Check your connection and retry.");
    } finally {
      setSubmitting(false);
    }
  }

  /** No credentials in the browser: /auth/demo signs in server-side and writes
   *  the session cookies, so all this has to do is navigate afterwards. */
  async function onDemo() {
    setFormError(null);
    setFieldErrors({});
    setDemoLoading(true);
    try {
      const response = await fetch("/auth/demo", { method: "POST" });
      if (!response.ok) {
        const body = (await response.json().catch(() => null)) as { error?: string } | null;
        setFormError(body?.error ?? "Couldn't open the demo account. Try again in a moment.");
        return;
      }
      router.replace(next);
      router.refresh();
    } catch {
      setFormError("Couldn't reach the authentication service. Check your connection and retry.");
    } finally {
      setDemoLoading(false);
    }
  }

  if (confirmationSentTo) {
    return (
      <ConfirmationPanel
        email={confirmationSentTo}
        onBack={() => {
          setConfirmationSentTo(null);
          setMode("signin");
          setFormError(null);
        }}
      />
    );
  }

  return (
    <div className="rounded-card border border-subtle bg-raised p-6 shadow-card">
      <div
        role="tablist"
        aria-label="Sign in or create an account"
        className="mb-6 grid grid-cols-2 gap-1 rounded-control bg-sunken p-1"
      >
        {(["signin", "signup"] as const).map((value) => (
          <button
            key={value}
            role="tab"
            type="button"
            aria-selected={mode === value}
            onClick={() => {
              setMode(value);
              setFieldErrors({});
              setFormError(null);
            }}
            className={cn(
              "h-8 rounded-[calc(var(--radius-control)-2px)] text-sm font-medium transition-colors",
              mode === value
                ? "bg-raised text-ink shadow-sm"
                : "text-ink-secondary hover:text-ink",
            )}
          >
            {value === "signin" ? "Sign in" : "Create account"}
          </button>
        ))}
      </div>

      <form onSubmit={onSubmit} noValidate className="flex flex-col gap-4">
        <fieldset disabled={submitting || demoLoading} className="flex flex-col gap-4">
          <TextField
            label="Email"
            type="email"
            autoComplete="email"
            autoFocus
            placeholder="you@example.com"
            value={email}
            error={fieldErrors.email ?? null}
            onChange={(event) => setEmail(event.target.value)}
          />
          <TextField
            label="Password"
            type="password"
            autoComplete={mode === "signup" ? "new-password" : "current-password"}
            placeholder="••••••••"
            value={password}
            error={fieldErrors.password ?? null}
            hint={mode === "signup" ? `At least ${MIN_PASSWORD_LENGTH} characters.` : undefined}
            onChange={(event) => setPassword(event.target.value)}
          />
        </fieldset>

        {formError && (
          <p
            role="alert"
            className="rounded-control border border-subtle bg-danger-wash px-3 py-2 text-sm text-ink"
          >
            {formError}
          </p>
        )}

        <Button
          type="submit"
          variant="primary"
          disabled={demoLoading}
          loading={submitting}
          loadingLabel={mode === "signup" ? "Creating account…" : "Signing in…"}
        >
          {mode === "signup" ? "Create account" : "Sign in"}
        </Button>
      </form>

      {demoAvailable && (
        <div className="mt-6">
          <div className="flex items-center gap-3" aria-hidden>
            <span className="h-px flex-1 bg-subtle" />
            <span className="text-xs text-ink-tertiary">or</span>
            <span className="h-px flex-1 bg-subtle" />
          </div>

          <Button
            type="button"
            variant="secondary"
            onClick={onDemo}
            disabled={submitting}
            loading={demoLoading}
            loadingLabel="Opening demo…"
            className="mt-4 w-full"
          >
            Try the demo account
          </Button>

          <p className="mt-2 text-xs leading-relaxed text-ink-tertiary">
            Signs you into a shared, pre-confirmed account — no email needed. Its conversations are
            visible to every other visitor, so treat it as public.
          </p>
        </div>
      )}

      {mode === "signup" && (
        <p className="mt-4 text-xs leading-relaxed text-ink-tertiary">
          Messages are answered by third-party free-tier providers. Some providers may use free-tier
          prompts to improve their models — don&apos;t send anything sensitive.
        </p>
      )}
    </div>
  );
}

function ConfirmationPanel({ email, onBack }: { email: string; onBack: () => void }) {
  return (
    <div className="rounded-card border border-subtle bg-raised p-6 text-center shadow-card">
      <div className="mx-auto flex size-11 items-center justify-center rounded-full bg-accent-wash text-accent">
        <svg viewBox="0 0 24 24" fill="none" className="size-5" aria-hidden>
          <rect x="3" y="5" width="18" height="14" rx="2" stroke="currentColor" strokeWidth="1.6" />
          <path d="m3.5 7 8.5 6 8.5-6" stroke="currentColor" strokeWidth="1.6" strokeLinejoin="round" />
        </svg>
      </div>

      <h2 className="mt-4 font-serif text-xl text-ink">Confirm your email</h2>
      <p className="mt-2 text-sm text-ink-secondary">
        We sent a link to <span className="font-medium text-ink">{email}</span>. Your account needs a
        confirmed address before the gateway will accept it.
      </p>
      <p className="mt-3 text-xs text-ink-tertiary">
        Nothing in your inbox? Check spam, then try signing in again to resend.
      </p>

      <div className="mt-6">
        <Button variant="secondary" onClick={onBack} className="w-full">
          Back to sign in
        </Button>
      </div>
    </div>
  );
}

function isUnconfirmedEmail(error: AuthError): boolean {
  return error.code === "email_not_confirmed" || /not confirmed/i.test(error.message);
}

/** Supabase's own wording is terse and occasionally cryptic; this is the layer
 *  that turns it into something a user can act on. Unknown codes fall through
 *  to the original message rather than to a generic — a real message the user
 *  can quote beats a friendly one that says nothing. */
function describeAuthError(error: AuthError, mode: Mode): string {
  if (error.status === 429 || error.code === "over_request_rate_limit") {
    return "Too many attempts. Wait a minute and try again.";
  }
  if (error.code === "invalid_credentials" || /invalid login credentials/i.test(error.message)) {
    return "That email and password don't match an account.";
  }
  if (error.code === "user_already_exists" || /already registered/i.test(error.message)) {
    return "There's already an account with that email. Try signing in instead.";
  }
  if (error.code === "weak_password") {
    return "That password is too weak. Try a longer one.";
  }
  if (error.code === "signup_disabled") {
    return "New sign-ups are disabled on this deployment.";
  }
  return mode === "signup"
    ? `Couldn't create the account: ${error.message}`
    : `Couldn't sign in: ${error.message}`;
}
