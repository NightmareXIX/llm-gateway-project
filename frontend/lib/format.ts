/** Small formatting helpers. Pure, so they are testable and safe to call during render. */

const MINUTE = 60_000;
const HOUR = 60 * MINUTE;
const DAY = 24 * HOUR;

/** "just now" / "12m" / "3h" / "Tue" / "14 Mar" — sidebar-width timestamps. */
export function relativeTime(iso: string, now: Date = new Date()): string {
  const then = new Date(iso);
  const delta = now.getTime() - then.getTime();

  if (Number.isNaN(delta)) return "";
  if (delta < MINUTE) return "just now";
  if (delta < HOUR) return `${Math.floor(delta / MINUTE)}m`;
  if (delta < DAY) return `${Math.floor(delta / HOUR)}h`;
  if (delta < 7 * DAY) return then.toLocaleDateString(undefined, { weekday: "short" });
  return then.toLocaleDateString(undefined, { day: "numeric", month: "short" });
}

export function absoluteTime(iso: string): string {
  const date = new Date(iso);
  return Number.isNaN(date.getTime())
    ? ""
    : date.toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" });
}

export type DateGroup = "Today" | "Yesterday" | "Previous 7 days" | "Older";

export function dateGroup(iso: string, now: Date = new Date()): DateGroup {
  const then = new Date(iso);
  const startOfToday = new Date(now.getFullYear(), now.getMonth(), now.getDate()).getTime();

  if (then.getTime() >= startOfToday) return "Today";
  if (then.getTime() >= startOfToday - DAY) return "Yesterday";
  if (then.getTime() >= startOfToday - 7 * DAY) return "Previous 7 days";
  return "Older";
}

export const GROUP_ORDER: DateGroup[] = ["Today", "Yesterday", "Previous 7 days", "Older"];

/**
 * A conversation title derived from its opening message.
 *
 * Phase 1 has no server-side title generation and `conversations.title` starts
 * null, so without this the sidebar is a column of identical rows. Cut on a word
 * boundary; strip newlines so a pasted block does not become a one-line title
 * with odd spacing.
 */
export const TITLE_MAX_CHARS = 60;

export function deriveTitle(text: string): string {
  const flat = text.replace(/\s+/g, " ").trim();
  if (flat.length <= TITLE_MAX_CHARS) return flat;

  const cut = flat.slice(0, TITLE_MAX_CHARS);
  const lastSpace = cut.lastIndexOf(" ");
  return `${(lastSpace > 24 ? cut.slice(0, lastSpace) : cut).trimEnd()}…`;
}

export function formatTokens(count: number): string {
  return count >= 1000 ? `${(count / 1000).toFixed(count >= 10_000 ? 0 : 1)}k` : String(count);
}
