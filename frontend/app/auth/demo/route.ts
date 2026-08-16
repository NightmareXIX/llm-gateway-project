import { NextResponse } from "next/server";
import { getSupabaseServerClient } from "@/lib/supabase/server";

/**
 * Signs the caller into the shared demo account.
 *
 * The sign-in happens here rather than in the browser on purpose: `DEMO_MAIL`
 * and `DEMO_PASSWORD` are server-only variables (no `NEXT_PUBLIC_` prefix), so
 * the credentials never ship in the client bundle. The route hands back nothing
 * but an ok/error — the session arrives as the auth cookies this handler writes,
 * exactly like /auth/callback does after a confirmation link.
 *
 * The account is shared and its conversations are visible to whoever presses the
 * button. That is the point of a portfolio demo, and the UI says so.
 */
export async function POST() {
  const email = process.env.DEMO_MAIL;
  const password = process.env.DEMO_PASSWORD;

  if (!email || !password) {
    return NextResponse.json(
      { error: "The demo account isn't configured on this deployment." },
      { status: 404 },
    );
  }

  const supabase = await getSupabaseServerClient();
  const { error } = await supabase.auth.signInWithPassword({ email, password });

  if (error) {
    // A broken demo account is an ops problem, not a user error — say so plainly
    // rather than making the visitor wonder what they typed wrong.
    return NextResponse.json(
      { error: `Couldn't open the demo account: ${error.message}` },
      { status: error.status ?? 502 },
    );
  }

  return NextResponse.json({ ok: true });
}
