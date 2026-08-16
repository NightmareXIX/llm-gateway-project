/**
 * The browser half of `GET /v1/models` (D21, Phase 3 Step 8).
 *
 * Written against the spec's three rules rather than the markup: a blocked
 * slot cannot be chosen and says when it comes back, an `unknown` slot is
 * chosen exactly like an `available` one, and picking a new value is the only
 * thing this component ever tells its caller.
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

describe("an available slot", () => {
  it("is selectable and carries no status suffix", () => {
    render(
      <ModelPicker
        value="auto"
        onChange={vi.fn()}
        models={models(entry({ id: "auto", status: "available" }))}
      />,
    );

    const option = screen.getByRole("option", { name: "auto" }) as HTMLOptionElement;
    expect(option.disabled).toBe(false);
  });
});

describe("an unknown slot", () => {
  it("renders selectable and unlabelled — not knowing is not a reason to block it", () => {
    render(
      <ModelPicker
        value="auto"
        onChange={vi.fn()}
        models={models(entry({ id: "fast", status: "unknown" }))}
      />,
    );

    const option = screen.getByRole("option", { name: "fast" }) as HTMLOptionElement;
    expect(option.disabled).toBe(false);
  });
});

describe("a rate_limited slot", () => {
  it("is disabled and its label names when it resets", () => {
    const resetsAt = new Date(Date.now() + 4 * 60_000).toISOString();
    render(
      <ModelPicker
        value="auto"
        onChange={vi.fn()}
        models={models(
          entry({ id: "auto", status: "available" }),
          entry({ id: "fast", status: "rate_limited", resets_at: resetsAt }),
        )}
      />,
    );

    const option = screen.getByRole("option", {
      name: "fast — rate limited, resets in ~4 min",
    }) as HTMLOptionElement;
    expect(option.disabled).toBe(true);
  });
});

describe("an unavailable slot", () => {
  it("is disabled and says so, even with no resets_at to report", () => {
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

    const option = screen.getByRole("option", {
      name: "general — unavailable",
    }) as HTMLOptionElement;
    expect(option.disabled).toBe(true);
  });
});

describe("selecting a value", () => {
  it("reports the new slot id and nothing else", () => {
    const onChange = vi.fn();
    render(
      <ModelPicker
        value="auto"
        onChange={onChange}
        models={models(
          entry({ id: "auto", status: "available" }),
          entry({ id: "fast", status: "available" }),
        )}
      />,
    );

    fireEvent.change(screen.getByLabelText("Model"), { target: { value: "fast" } });

    expect(onChange).toHaveBeenCalledWith("fast");
  });
});

describe("before the fetch resolves", () => {
  it("still renders the current value rather than disappearing", () => {
    render(<ModelPicker value="auto" onChange={vi.fn()} models={undefined} />);

    expect(screen.getByRole("option", { name: "auto" })).toBeInTheDocument();
  });
});
