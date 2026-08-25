/**
 * Two structural promises the button's own API makes.
 *
 * Both were quietly broken and only visible on the one button that exercises
 * them — the sidebar's "New conversation", whose icon rendered on its own line
 * above its label. The cause was a wrapper `<span>` that turned an icon and its
 * text into a single flex item; `svg` is a block element under preflight, so it
 * took a line of its own and the `gap-*` in `SIZES` had nothing left to space.
 * The radius was the same class of bug one layer up: `cn` is a plain join, so
 * `rounded-card` from a caller landed *beside* the base `rounded-control` and
 * the stylesheet's ordering decided which won.
 */

import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { Button } from "@/components/ui/Button";

describe("Button", () => {
  it("makes each child its own flex item, so an icon sits beside its label", () => {
    render(
      <Button>
        <svg data-testid="icon" viewBox="0 0 24 24" aria-hidden />
        New conversation
      </Button>,
    );

    const button = screen.getByRole("button", { name: "New conversation" });
    // A direct child: nothing wraps the icon and the label together.
    expect(screen.getByTestId("icon").parentElement).toBe(button);
    expect(button.querySelector("span")).toBeNull();
  });

  it("emits exactly one radius class, chosen by `shape`", () => {
    const { rerender } = render(<Button>Go</Button>);
    let button = screen.getByRole("button");
    expect(button.className).toContain("rounded-control");
    expect(button.className).not.toContain("rounded-card");

    rerender(<Button shape="card">Go</Button>);
    button = screen.getByRole("button");
    expect(button.className).toContain("rounded-card");
    expect(button.className).not.toContain("rounded-control");
  });

  it("still swaps in the loading label, and keeps the children otherwise", () => {
    const { rerender } = render(
      <Button loading loadingLabel="Sending…">
        Send
      </Button>,
    );

    const button = screen.getByRole("button", { name: /Sending/ });
    expect(button).toHaveAttribute("aria-busy", "true");
    expect(button).toBeDisabled();
    // The label replaced the children rather than joining them.
    expect(button.textContent).toBe("Sending…");

    rerender(<Button loading>Send</Button>);
    // No label given: the children stay put and only the spinner is added.
    expect(screen.getByRole("button").textContent).toContain("Send");
  });
});
