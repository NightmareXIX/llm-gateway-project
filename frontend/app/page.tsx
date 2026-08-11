import { redirect } from "next/navigation";
import { getSupabaseServerClient } from "@/lib/supabase/server";

/**
 * The root is a signpost, not a page.
 *
 * Resolved on the server so a signed-in user never sees a landing flash before
 * being moved along, and a signed-out one never sees the chat shell at all.
 */
export default async function RootPage() {
  const supabase = await getSupabaseServerClient();
  const {
    data: { user },
  } = await supabase.auth.getUser();

  redirect(user ? "/chat" : "/login");
}
