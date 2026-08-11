"use client";

import { useEffect, useState } from "react";

export type Theme = "light" | "dark" | "system";

export const THEMES: Theme[] = ["light", "dark", "system"];

export const THEME_LABELS: Record<Theme, string> = {
  light: "Light",
  dark: "Dark",
  system: "System",
};

/**
 * Apply a theme choice to the document and remember it.
 *
 * "system" is stored as the *absence* of a preference, which is what lets the
 * blocking script in `app/layout.tsx` reproduce this decision before first paint
 * with three lines and no state machine.
 */
export function applyTheme(theme: Theme) {
  const dark =
    theme === "dark" ||
    (theme === "system" && window.matchMedia("(prefers-color-scheme: dark)").matches);

  document.documentElement.classList.toggle("dark", dark);
  if (theme === "system") localStorage.removeItem("theme");
  else localStorage.setItem("theme", theme);
}

/**
 * The current theme choice, plus a setter.
 *
 * `mounted` is part of the contract: the stored choice lives in localStorage and
 * `matchMedia`, neither of which exists during SSR, so a control that renders
 * the real state on the server is a guaranteed hydration mismatch. Callers
 * render a same-sized placeholder until it flips.
 */
export function useTheme() {
  const [theme, setTheme] = useState<Theme>("system");
  const [mounted, setMounted] = useState(false);

  useEffect(() => {
    const stored = localStorage.getItem("theme");
    setTheme(stored === "light" || stored === "dark" ? stored : "system");
    setMounted(true);
  }, []);

  useEffect(() => {
    if (!mounted || theme !== "system") return;
    // Keep following the OS for as long as "system" is the choice.
    const media = window.matchMedia("(prefers-color-scheme: dark)");
    const onChange = () => applyTheme("system");
    media.addEventListener("change", onChange);
    return () => media.removeEventListener("change", onChange);
  }, [mounted, theme]);

  return {
    theme,
    mounted,
    select(next: Theme) {
      setTheme(next);
      applyTheme(next);
    },
  };
}
