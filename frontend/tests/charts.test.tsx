/**
 * The three chart primitives, asserted on the SVG they actually produce.
 *
 * That is the point of D51's "no chart library": these are pure functions from
 * an array of numbers to markup, so a test can check the geometry itself
 * rather than checking that a mocked canvas was handed the right props. Every
 * case below is either a known array whose coordinates can be computed by hand
 * or one of the three degenerate inputs a dashboard's first day is made of —
 * an empty series, an all-zero series, and a zero denominator.
 */

import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { BarRow } from "@/components/charts/BarRow";
import { Meter, formatPercent } from "@/components/charts/Meter";
import { Sparkline, areaPath, linePath } from "@/components/charts/Sparkline";

describe("Sparkline", () => {
  it("draws one point per bucket, on a scale set by the largest", () => {
    render(<Sparkline values={[0, 5, 10]} ariaLabel="Requests" />);

    const svg = screen.getByRole("img", { name: "Requests" });
    const line = svg.querySelector("path[fill='none']");

    // Three buckets across a 100-wide viewBox: 0, 50, 100. The vertical scale
    // is 1..31 (one unit of headroom at the top), so 0 → 31 and 10 → 1.
    expect(line).toHaveAttribute("d", "M0.00 31.00 L50.00 16.00 L100.00 1.00");
    expect(svg).not.toHaveAttribute("data-empty");
  });

  it("renders a baseline and nothing else for an empty series", () => {
    render(<Sparkline values={[]} ariaLabel="Requests" />);

    const svg = screen.getByRole("img", { name: "Requests" });
    expect(svg).toHaveAttribute("data-empty", "true");
    expect(svg.querySelector("path")).toBeNull();
    expect(svg.querySelector("line")).not.toBeNull();
  });

  it("draws an all-zero window as a flat floor, not as full height", () => {
    // The divisor is clamped to 1, so a quiet window reads as quiet rather
    // than as `NaN` or — worse — as a band suggesting traffic there was none of.
    render(<Sparkline values={[0, 0, 0]} ariaLabel="Requests" />);

    const line = screen.getByRole("img", { name: "Requests" }).querySelector("path[fill='none']");
    expect(line).toHaveAttribute("d", "M0.00 31.00 L50.00 31.00 L100.00 31.00");
  });

  it("puts a single bucket in the middle rather than at the left edge", () => {
    expect(linePath([4], 4)).toBe("M50.00 1.00");
  });

  it("closes the area path down to the baseline at both ends", () => {
    expect(areaPath([0, 10], 10)).toBe("M0.00 32 L0.00 31.00 L100.00 1.00 L100.00 32 Z");
  });

  it("draws an overlay series on the same scale as the values", () => {
    render(<Sparkline values={[10, 10]} overlay={[0, 10]} ariaLabel="Requests" />);

    const paths = screen
      .getByRole("img", { name: "Requests" })
      .querySelectorAll("path[fill='none']");
    expect(paths).toHaveLength(2);
    expect(paths[1]).toHaveAttribute("d", "M0.00 31.00 L100.00 1.00");
  });

  it("ignores an overlay whose length disagrees with the series", () => {
    // Two series over different bucket counts share no x-axis. Drawing them
    // anyway would be a lie that looks like a chart.
    render(<Sparkline values={[1, 2, 3]} overlay={[1]} ariaLabel="Requests" />);

    expect(
      screen.getByRole("img", { name: "Requests" }).querySelectorAll("path[fill='none']"),
    ).toHaveLength(1);
  });
});

describe("BarRow", () => {
  it("scales the bar against the row set's own maximum", () => {
    render(<BarRow label="Groq" value={5} max={20} valueLabel="5 calls" />);

    const bars = screen.getByRole("img").querySelectorAll("rect");
    expect(bars).toHaveLength(2);
    expect(bars[1]).toHaveAttribute("width", "25.00");
    expect(screen.getByText("5 calls")).toBeInTheDocument();
  });

  it("renders an empty track rather than a NaN width when there is no scale", () => {
    render(<BarRow label="Groq" value={0} max={0} valueLabel="0 calls" />);

    const bars = screen.getByRole("img").querySelectorAll("rect");
    expect(bars).toHaveLength(1);
    expect(bars[0]).toHaveAttribute("width", "100");
  });

  it("never draws past the track, even if a value exceeds the maximum", () => {
    render(<BarRow label="Groq" value={30} max={20} valueLabel="30" />);

    const bars = screen.getByRole("img").querySelectorAll("rect");
    expect(bars[1]).toHaveAttribute("width", "100.00");
  });
});

describe("Meter", () => {
  it("renders a rate over a real denominator", () => {
    render(<Meter label="Error rate" value={1} total={4} />);

    expect(screen.getByText("25%")).toBeInTheDocument();
    expect(screen.getByRole("img", { name: "Error rate: 25%" })).toBeInTheDocument();
  });

  it("renders an em dash and an empty track when nothing has happened yet", () => {
    // The whole reason this component exists: three rates share this shape and
    // all three have a zero denominator on a brand-new account.
    render(<Meter label="Error rate" value={0} total={0} />);

    expect(screen.getByText("—")).toBeInTheDocument();
    expect(screen.queryByText(/NaN/)).toBeNull();
    expect(screen.getByRole("img", { name: "Error rate: no data" }).querySelectorAll("rect"))
      .toHaveLength(1);
  });

  it("distinguishes a real zero rate from no data at all", () => {
    render(<Meter label="Error rate" value={0} total={12} />);

    expect(screen.getByText("0%")).toBeInTheDocument();
    expect(screen.queryByText("—")).toBeNull();
  });

  it("keeps a decimal on a small but non-zero rate", () => {
    // A 0.4% error rate is a different fact from a 0% one, and rounding it
    // away rounds in the flattering direction.
    expect(formatPercent(0.004)).toBe("0.4%");
    expect(formatPercent(0)).toBe("0%");
    expect(formatPercent(1)).toBe("100%");
  });
});
