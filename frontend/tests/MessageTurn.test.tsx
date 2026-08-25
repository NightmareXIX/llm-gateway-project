/**
 * Model answers are markdown; user text is not.
 *
 * Every provider in the fleet answers in markdown, and until this landed the
 * transcript rendered that verbatim — a GFM table arrived as a column of pipes
 * and `**bold**` arrived with its asterisks. These tests pin both halves of the
 * fix: an assistant turn is parsed, and a user turn is still shown exactly as
 * it was typed, because someone who writes `**hi**` meant the asterisks.
 *
 * Asserted on the DOM the renderer produces (a real `<table>`, a real `<li>`)
 * rather than on the markdown library's internals, so swapping the renderer
 * later is a refactor rather than a test rewrite.
 */

import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { MessageTurn } from "@/components/MessageTurn";
import type { Message } from "@/lib/types";

function message(role: Message["role"], text: string): Message {
  return {
    id: "0d5a1f2e-0000-4000-8000-000000000001",
    seq: 1,
    role,
    content: [{ type: "text", text }],
    meta: {},
    created_at: "2026-08-22T22:22:00Z",
  } as Message;
}

describe("an assistant turn", () => {
  it("renders a GFM table as a table, not as a row of pipes", () => {
    const { container } = render(
      <MessageTurn
        message={message(
          "assistant",
          ["| Aspect | Details |", "| --- | --- |", "| Cutoff | May 2026 |"].join("\n"),
        )}
      />,
    );

    expect(container.querySelector("table")).not.toBeNull();
    expect(screen.getByRole("columnheader", { name: "Aspect" })).toBeInTheDocument();
    expect(screen.getByRole("cell", { name: "May 2026" })).toBeInTheDocument();
    expect(container.textContent).not.toContain("|");
  });

  it("renders headings, emphasis and lists", () => {
    const { container } = render(
      <MessageTurn
        message={message(
          "assistant",
          ["## A rundown", "", "- **Strengths** — explaining things", "- Limitations"].join("\n"),
        )}
      />,
    );

    expect(screen.getByRole("heading", { name: "A rundown" })).toBeInTheDocument();
    expect(container.querySelectorAll("li")).toHaveLength(2);
    expect(container.querySelector("strong")?.textContent).toBe("Strengths");
    expect(container.textContent).not.toContain("**");
  });

  it("renders a fenced block as code", () => {
    const { container } = render(
      <MessageTurn message={message("assistant", "```python\nprint('hi')\n```")} />,
    );

    const code = container.querySelector("pre code");
    expect(code?.textContent).toContain("print('hi')");
    expect(container.textContent).not.toContain("```");
  });

  it("opens links in a new tab without handing the opener over", () => {
    render(<MessageTurn message={message("assistant", "[docs](https://example.com/x)")} />);

    const link = screen.getByRole("link", { name: "docs" });
    expect(link).toHaveAttribute("href", "https://example.com/x");
    expect(link).toHaveAttribute("target", "_blank");
    expect(link.getAttribute("rel")).toContain("noopener");
  });

  it("leaves HTML in the model's answer as inert text", () => {
    // The text comes from a third-party provider over an API the user's own
    // prompt steers. `rehype-raw` is deliberately not installed, so this must
    // never become DOM.
    const { container } = render(
      <MessageTurn message={message("assistant", "<img src=x onerror=alert(1)> done")} />,
    );

    expect(container.querySelector("img")).toBeNull();
    // Escaped into the text node it came in as, attribute and all.
    expect(container.innerHTML).toContain("&lt;img src=x onerror=alert(1)&gt;");
  });
});

describe("a user turn", () => {
  it("is shown exactly as it was typed", () => {
    const { container } = render(<MessageTurn message={message("user", "**hi** | not a table")} />);

    expect(container.querySelector("table")).toBeNull();
    expect(container.querySelector("strong")).toBeNull();
    expect(screen.getByText("**hi** | not a table")).toBeInTheDocument();
  });
});
