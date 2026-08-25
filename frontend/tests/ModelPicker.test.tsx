/**
 * The browser half of `GET /v1/models` (D21, Phase 3 Step 8).
 *
 * Written against the spec's three rules rather than the markup: a blocked
 * slot cannot be chosen and says when it comes back, an `unknown` slot is
 * chosen exactly like an `available` one, and picking a new value is the only
 * thing this component ever tells its caller. Those rules outlived the native
 * `<select>` this picker used to be, so the assertions below are still about
 * the *option* a user can or cannot choose — what changed is that the options
 * exist only while the popup is open, and that "disabled" is now
 * `aria-disabled` plus an inert click rather than one attribute the platform
 * enforced for us.
 */

import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { ModelPicker } from "@/components/ModelPicker";
import type { ModelEntry, ModelsResponse } from "@/lib/types";

function entry(overrides: Partial<ModelEntry>): ModelEntry {
  return {
    id: "auto",
    object: "model",
    created: 0,
    owned_by: null,
    status: "available",
    resets_at: null,
    description: "",
    candidates: [],
    ...overrides,
  };
}

function models(...entries: ModelEntry[]): ModelsResponse {
  return { object: "list", data: entries };
}

/** The trigger, by its accessible name — "Model" plus the current value. */
function trigger(): HTMLElement {
  return screen.getByRole("combobox", { name: /Model/ });
}

function openPicker(): HTMLElement {
  const control = trigger();
  fireEvent.click(control);
  return control;
}

describe("the collapsed control", () => {
  it("names the current slot and opens its list on click", () => {
    render(
      <ModelPicker
        value="auto"
        onChange={vi.fn()}
        models={models(entry({ id: "auto" }), entry({ id: "fast" }))}
      />,
    );

    const control = trigger();
    expect(control).toHaveAccessibleName("Model auto");
    expect(control).toHaveAttribute("aria-expanded", "false");
    expect(screen.queryByRole("listbox")).not.toBeInTheDocument();

    fireEvent.click(control);

    expect(control).toHaveAttribute("aria-expanded", "true");
    expect(screen.getByRole("listbox")).toBeInTheDocument();
  });
});

describe("an available slot", () => {
  it("is selectable and carries no status suffix", () => {
    render(
      <ModelPicker
        value="auto"
        onChange={vi.fn()}
        models={models(entry({ id: "auto", status: "available" }))}
      />,
    );
    openPicker();

    const option = screen.getByRole("option", { name: "auto" });
    expect(option).not.toHaveAttribute("aria-disabled");
    expect(option).toHaveAttribute("aria-selected", "true");
  });
});

describe("an unknown slot", () => {
  it("renders selectable and unlabelled — not knowing is not a reason to block it", () => {
    const onChange = vi.fn();
    render(
      <ModelPicker
        value="auto"
        onChange={onChange}
        models={models(entry({ id: "fast", status: "unknown" }))}
      />,
    );
    openPicker();

    const option = screen.getByRole("option", { name: "fast" });
    expect(option).not.toHaveAttribute("aria-disabled");

    fireEvent.click(option);
    expect(onChange).toHaveBeenCalledWith("fast");
  });
});

describe("a rate_limited slot", () => {
  it("is unselectable and its label names when it resets", () => {
    const onChange = vi.fn();
    const resetsAt = new Date(Date.now() + 4 * 60_000).toISOString();
    render(
      <ModelPicker
        value="auto"
        onChange={onChange}
        models={models(
          entry({ id: "auto", status: "available" }),
          entry({ id: "fast", status: "rate_limited", resets_at: resetsAt }),
        )}
      />,
    );
    openPicker();

    const option = screen.getByRole("option", {
      name: "fast — rate limited, resets in ~4 min",
    });
    expect(option).toHaveAttribute("aria-disabled", "true");

    // Inert to a click, and the popup stays open — nothing was chosen.
    fireEvent.click(option);
    expect(onChange).not.toHaveBeenCalled();
    expect(screen.getByRole("listbox")).toBeInTheDocument();
  });
});

describe("an unavailable slot", () => {
  it("is unselectable and says so, even with no resets_at to report", () => {
    render(
      <ModelPicker
        value="auto"
        onChange={vi.fn()}
        models={models(
          entry({ id: "auto", status: "available" }),
          entry({ id: "general", status: "unavailable", resets_at: null }),
        )}
      />,
    );
    openPicker();

    expect(screen.getByRole("option", { name: "general — unavailable" })).toHaveAttribute(
      "aria-disabled",
      "true",
    );
  });
});

describe("selecting a value", () => {
  it("reports the new slot id and nothing else, then closes", () => {
    const onChange = vi.fn();
    render(
      <ModelPicker
        value="auto"
        onChange={onChange}
        models={models(entry({ id: "auto" }), entry({ id: "fast" }))}
      />,
    );
    openPicker();

    fireEvent.click(screen.getByRole("option", { name: "fast" }));

    expect(onChange).toHaveBeenCalledExactlyOnceWith("fast");
    expect(screen.queryByRole("listbox")).not.toBeInTheDocument();
  });

  it("says nothing when the slot picked is the one already selected", () => {
    const onChange = vi.fn();
    render(<ModelPicker value="auto" onChange={onChange} models={models(entry({ id: "auto" }))} />);
    openPicker();

    fireEvent.click(screen.getByRole("option", { name: "auto" }));

    expect(onChange).not.toHaveBeenCalled();
    expect(screen.queryByRole("listbox")).not.toBeInTheDocument();
  });
});

describe("the keyboard", () => {
  it("opens on ArrowDown, walks past blocked slots, and chooses with Enter", () => {
    const onChange = vi.fn();
    render(
      <ModelPicker
        value="auto"
        onChange={onChange}
        models={models(
          entry({ id: "auto" }),
          entry({ id: "fast", status: "unavailable" }),
          entry({ id: "general" }),
        )}
      />,
    );

    fireEvent.keyDown(trigger(), { key: "ArrowDown" });
    const list = screen.getByRole("listbox");

    // From `auto`, one step down skips the unavailable `fast` entirely.
    fireEvent.keyDown(list, { key: "ArrowDown" });
    expect(list).toHaveAttribute(
      "aria-activedescendant",
      screen.getByRole("option", { name: "general" }).id,
    );

    fireEvent.keyDown(list, { key: "Enter" });
    expect(onChange).toHaveBeenCalledExactlyOnceWith("general");
  });

  it("closes on Escape without choosing anything", () => {
    const onChange = vi.fn();
    render(
      <ModelPicker
        value="auto"
        onChange={onChange}
        models={models(entry({ id: "auto" }), entry({ id: "fast" }))}
      />,
    );
    openPicker();

    fireEvent.keyDown(screen.getByRole("listbox"), { key: "Escape" });

    expect(screen.queryByRole("listbox")).not.toBeInTheDocument();
    expect(onChange).not.toHaveBeenCalled();
    expect(trigger()).toHaveFocus();
  });
});

describe("a click outside", () => {
  it("dismisses the list", () => {
    render(
      <ModelPicker
        value="auto"
        onChange={vi.fn()}
        models={models(entry({ id: "auto" }), entry({ id: "fast" }))}
      />,
    );
    openPicker();

    fireEvent.pointerDown(document.body);

    expect(screen.queryByRole("listbox")).not.toBeInTheDocument();
  });
});

describe("before the fetch resolves", () => {
  it("still renders the current value rather than disappearing", () => {
    render(<ModelPicker value="auto" onChange={vi.fn()} models={undefined} />);

    expect(trigger()).toHaveAccessibleName("Model auto");

    openPicker();
    expect(screen.getByRole("option", { name: "auto" })).toBeInTheDocument();
  });

  it("keeps a value the fetch never returned beside the ones it did", () => {
    render(
      <ModelPicker value="legacy" onChange={vi.fn()} models={models(entry({ id: "auto" }))} />,
    );
    openPicker();

    expect(screen.getByRole("option", { name: "legacy" })).toHaveAttribute("aria-selected", "true");
    expect(screen.getByRole("option", { name: "auto" })).toBeInTheDocument();
  });
});

describe("a disabled picker", () => {
  it("cannot be opened", () => {
    render(<ModelPicker value="auto" onChange={vi.fn()} models={models(entry({}))} disabled />);

    fireEvent.click(trigger());

    expect(screen.queryByRole("listbox")).not.toBeInTheDocument();
  });
});
