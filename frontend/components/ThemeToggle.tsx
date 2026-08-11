"use client";

import { THEMES, THEME_LABELS, useTheme } from "@/lib/theme";
import { IconButton } from "./ui/IconButton";

/**
 * A compact theme cycler, for surfaces with no account menu — which today means
 * the login page only. Inside the app, appearance lives in the account modal as
 * a three-way segmented control, where the current choice is visible without
 * having to press anything.
 *
 * Renders a same-sized placeholder until mounted: the choice lives in
 * localStorage and `matchMedia`, so rendering the real icon on the server would
 * guarantee a hydration mismatch.
 */
export function ThemeToggle() {
  const { theme, mounted, select } = useTheme();

  if (!mounted) return <span className="size-8" aria-hidden />;

  const next = THEMES[(THEMES.indexOf(theme) + 1) % THEMES.length] ?? "system";

  return (
    <IconButton
      label={`Theme: ${THEME_LABELS[theme]}. Switch to ${THEME_LABELS[next].toLowerCase()}.`}
      onClick={() => select(next)}
    >
      {theme === "system" ? <MonitorIcon /> : theme === "light" ? <SunIcon /> : <MoonIcon />}
    </IconButton>
  );
}

function SunIcon() {
  return (
    <svg viewBox="0 0 24 24" fill="none" className="size-4" aria-hidden>
      <circle cx="12" cy="12" r="4" stroke="currentColor" strokeWidth="1.6" />
      <path
        d="M12 3v2m0 14v2M3 12h2m14 0h2M5.6 5.6l1.4 1.4m10 10 1.4 1.4m0-12.8-1.4 1.4m-10 10-1.4 1.4"
        stroke="currentColor"
        strokeWidth="1.6"
        strokeLinecap="round"
      />
    </svg>
  );
}

function MoonIcon() {
  return (
    <svg viewBox="0 0 24 24" fill="none" className="size-4" aria-hidden>
      <path
        d="M20 14.5A8.5 8.5 0 0 1 9.5 4a8.5 8.5 0 1 0 10.5 10.5Z"
        stroke="currentColor"
        strokeWidth="1.6"
        strokeLinejoin="round"
      />
    </svg>
  );
}

function MonitorIcon() {
  return (
    <svg viewBox="0 0 24 24" fill="none" className="size-4" aria-hidden>
      <rect x="3" y="4" width="18" height="12" rx="2" stroke="currentColor" strokeWidth="1.6" />
      <path d="M9 20h6m-3-4v4" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" />
    </svg>
  );
}
