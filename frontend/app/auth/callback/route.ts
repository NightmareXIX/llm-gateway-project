import { NextResponse, type NextRequest } from "next/server";
import { getSupabaseServerClient } from "@/lib/supabase/server";

/**
 * Where the email-confirmation link lands.
 *
 * The gateway runs with `REQUIRE_VERIFIED_EMAIL=true`, so this route is on the
 * critical path of every new account: Supabase sends the user here with a
 * one-time `code`, this exchanges it for a session, and only then does a token
 * exist that the backend will accept.
 *
 * A failed exchange goes back to /login with a reason rather than to an error
 * page — an expired or already-used link is a normal thing to do (clicking the
 * mail twice), and the recovery is "sign in", which is exactly where this sends.
 */
export async function GET(request: NextRequest) {
  const { searchParams, origin } = request.nextUrl;
  const code = searchParams.get("code");
  const next = searchParams.get("next") ?? "/chat";

  if (!code) {
    return NextResponse.redirect(`${origin}/login?reason=missing_code`);
  }

  const supabase = await getSupabaseServerClient();
  const { error } = await supabase.auth.exchangeCodeForSession(code);

  if (error) {
    return NextResponse.redirect(`${origin}/login?reason=link_expired`);
  }

  return NextResponse.redirect(`${origin}${next.startsWith("/") ? next : "/chat"}`);
}
