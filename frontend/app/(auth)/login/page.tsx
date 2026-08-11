import { Suspense } from "react";
import { LoginForm } from "@/components/LoginForm";
import { ThemeToggle } from "@/components/ThemeToggle";
import { Skeleton } from "@/components/ui/Skeleton";

export const metadata = { title: "Sign in · LLM Gateway" };

/**
 * The login screen: a single centred card on the ground colour.
 *
 * No marketing panel, no split hero. This is the first screen of a tool, and
 * the only thing a returning user wants from it is the fastest possible path
 * back to their conversations.
 */
export default function LoginPage() {
  return (
    <main className="relative flex min-h-[100dvh] flex-col items-center justify-center px-4 py-12">
      <div className="absolute right-4 top-4">
        <ThemeToggle />
      </div>

      <div className="w-full max-w-[26rem]">
        <div className="mb-8 text-center">
          <Wordmark />
          <h1 className="mt-6 font-serif text-3xl leading-tight text-ink">
            One thread, many models
          </h1>
          <p className="mt-2 text-sm text-ink-secondary">
            Your conversations live here, not with the provider that happens to answer them.
          </p>
        </div>

        <Suspense fallback={<Skeleton className="h-[22rem] w-full rounded-card" />}>
          <LoginForm />
        </Suspense>
      </div>
    </main>
  );
}

function Wordmark() {
  return (
    <div className="inline-flex items-center gap-2 text-ink">
      <svg viewBox="0 0 24 24" fill="none" className="size-5 text-accent" aria-hidden>
        <path
          d="M12 2.5 21 7v10l-9 4.5L3 17V7l9-4.5Z"
          stroke="currentColor"
          strokeWidth="1.6"
          strokeLinejoin="round"
        />
        <path d="M12 12 3 7m9 5 9-5m-9 5v9.5" stroke="currentColor" strokeWidth="1.6" />
      </svg>
      <span className="text-sm font-semibold tracking-tight">LLM Gateway</span>
    </div>
  );
}
