import { UsageDashboard } from "@/components/UsageDashboard";

export const metadata = { title: "Usage · LLM Gateway" };

/**
 * The dashboard, at its own route rather than inside the chat shell.
 *
 * `/chat` holds itself to exactly `100dvh` and scrolls only its transcript
 * pane; this page is an ordinary document that scrolls, so wrapping it in that
 * shell would mean fighting the one layout rule that route depends on. It
 * links back rather than nesting, and the session guard for it lives in
 * `middleware.ts` beside `/chat`'s.
 */
export default function UsagePage() {
  return <UsageDashboard />;
}
