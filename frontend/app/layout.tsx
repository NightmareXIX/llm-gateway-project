import type { Metadata, Viewport } from "next";
import { Inter, Source_Serif_4 } from "next/font/google";
import "./globals.css";

/**
 * Two families, self-hosted at build time by `next/font` — no runtime font CDN,
 * no layout shift from a late swap.
 *
 * Inter carries the interface and body copy. Source Serif appears only at
 * display sizes (the login heading, the empty states): it is what makes the
 * product read as a place to write rather than a control panel, and used
 * sparingly it stays a signal instead of decoration. Metadata — model names,
 * token counts, request ids — uses the system mono stack, so a wire identifier
 * is visibly an identifier.
 */
const inter = Inter({
  subsets: ["latin"],
  variable: "--font-inter",
  display: "swap",
});

const sourceSerif = Source_Serif_4({
  subsets: ["latin"],
  variable: "--font-source-serif",
  display: "swap",
  weight: ["400", "600"],
});

export const metadata: Metadata = {
  title: "LLM Gateway",
  description: "One chat interface over several free-tier model providers.",
};

export const viewport: Viewport = {
  themeColor: [
    { media: "(prefers-color-scheme: light)", color: "#faf9f7" },
    { media: "(prefers-color-scheme: dark)", color: "#14130f" },
  ],
};

/**
 * Applied before first paint, which is the whole point of putting it here as a
 * blocking inline script: a theme resolved in an effect flashes the wrong one
 * for a frame, and on a dark-theme user that frame is a white screen.
 */
const THEME_SCRIPT = `
(function () {
  try {
    var stored = localStorage.getItem("theme");
    var dark = stored === "dark" ||
      (stored !== "light" && window.matchMedia("(prefers-color-scheme: dark)").matches);
    if (dark) document.documentElement.classList.add("dark");
  } catch (e) {}
})();
`;

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en" suppressHydrationWarning>
      <head>
        <script dangerouslySetInnerHTML={{ __html: THEME_SCRIPT }} />
      </head>
      <body className={`${inter.variable} ${sourceSerif.variable} antialiased`}>{children}</body>
    </html>
  );
}
