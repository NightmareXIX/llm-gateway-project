"use client";

import { createBrowserClient } from "@supabase/ssr";

/**
 * The browser Supabase client.
 *
 * Cookie-backed (that is what `@supabase/ssr` buys over the plain JS client)
 * so `middleware.ts` can read the session on the server and guard `/chat/*`
 * before a page renders. Without it the app would flash the chat shell and then
 * redirect, which reads as a bug.
 *
 * Memoized: `createBrowserClient` is cheap but each call spins up its own auth
 * listener, and two listeners racing a token refresh is a real source of
 * "randomly logged out".
 */
let cached: ReturnType<typeof createBrowserClient> | null = null;

export function getSupabaseBrowserClient() {
  if (cached) return cached;

  const url = process.env.NEXT_PUBLIC_SUPABASE_URL;
  const anonKey = process.env.NEXT_PUBLIC_SUPABASE_ANON_KEY;

  if (!url || !anonKey) {
    // Loud, at first use, naming the variable — the same contract as the
    // backend's config module. A silent undefined here surfaces as an
    // inscrutable 401 three screens later.
    throw new Error(
      "Missing NEXT_PUBLIC_SUPABASE_URL or NEXT_PUBLIC_SUPABASE_ANON_KEY. See .env.local.example.",
    );
  }

  cached = createBrowserClient(url, anonKey);
  return cached;
}
