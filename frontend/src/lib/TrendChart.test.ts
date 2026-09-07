import { describe, it, expect, afterEach, beforeAll } from "vitest";
import { render, cleanup, waitFor } from "@testing-library/svelte";
import TrendChart from "./TrendChart.svelte";

// The chart reads its scroll container's clientWidth to fill the width; jsdom
// reports 0, so we stub it (ResizeObserver is absent in jsdom, but the chart
// guards for that and falls back to a one-shot clientWidth read on mount).
let mockClientWidth = 0;
beforeAll(() => {
  Object.defineProperty(HTMLElement.prototype, "clientWidth", {
    configurable: true,
    get() {
      return mockClientWidth;
    },
  });
});

describe("TrendChart", () => {
  afterEach(() => {
    cleanup();
    mockClientWidth = 0;
  });

  it("renders an svg with one point per series entry", () => {
    const { container } = render(TrendChart, {
      props: {
        series: [
          {
            name: "Corvette Z06",
            points: [
              { x: 1, label: "9/1", value: 216.5 },
              { x: 2, label: "9/2", value: 214.0 },
              { x: 3, label: "9/3", value: 213.2 },
            ],
          },
        ],
      },
    });

    const svg = container.querySelector("svg");
    expect(svg).toBeTruthy();
    expect(container.querySelectorAll("circle").length).toBe(3);
  });

  it("spreads same-day sessions to distinct x positions (no stacking)", () => {
    const { container } = render(TrendChart, {
      props: {
        series: [
          {
            name: "Corvette Z06",
            points: [
              // three sessions on the same calendar day, distinct timestamps
              { x: 1000, label: "9/3", value: 216.5 },
              { x: 2000, label: "9/3", value: 215.0 },
              { x: 3000, label: "9/3", value: 214.2 },
            ],
          },
        ],
      },
    });

    const cx = [...container.querySelectorAll("circle")].map((c) => c.getAttribute("cx"));
    // All three render at DIFFERENT x — placed by their real timestamp.
    expect(new Set(cx).size).toBe(3);
  });

  it("renders one path per series for multiple cars", () => {
    const { container } = render(TrendChart, {
      props: {
        series: [
          { name: "Corvette Z06", points: [{ x: 1, label: "9/1", value: 216.5 }, { x: 2, label: "9/2", value: 214.0 }] },
          { name: "Ferrari 296", points: [{ x: 1, label: "9/1", value: 220.1 }, { x: 2, label: "9/2", value: 218.0 }] },
        ],
      },
    });

    expect(container.querySelectorAll("path").length).toBe(2);
    expect(container.querySelectorAll("circle").length).toBe(4);
  });

  it("raising the zoom prop widens the plot (svg width grows, animated)", async () => {
    const series = [
      { name: "Corvette Z06", points: [{ x: 1, label: "9/1", value: 216.5 }, { x: 2, label: "9/2", value: 214.0 }] },
    ];
    const { container, rerender } = render(TrendChart, { props: { series, zoom: 1 } });
    const before = Number(container.querySelector("svg")!.getAttribute("width"));
    await rerender({ series, zoom: 3 });
    // Width is tweened, so it grows over a few animation frames rather than at once.
    await waitFor(() => {
      const after = Number(container.querySelector("svg")!.getAttribute("width"));
      expect(after).toBeGreaterThan(before);
    });
  });

  it("fills the container width at base zoom", () => {
    mockClientWidth = 640;
    const day = 86400;
    const start = 1_725_000_000; // some unix-seconds instant
    const { container } = render(TrendChart, {
      props: {
        series: [
          {
            name: "Corvette Z06",
            points: [
              { x: start, label: "d1", value: 216.5 },
              { x: start + day, label: "d2", value: 215.0 },
              { x: start + 2 * day, label: "d3", value: 214.2 },
            ],
          },
        ],
      },
    });

    const w = Number(container.querySelector("svg")!.getAttribute("width"));
    // At base zoom the svg spans ~the container, not a tiny fixed width.
    expect(Math.abs(w - 640)).toBeLessThan(2);
  });

  it("shows sub-day (HH:MM) time labels when zoomed into a single day", () => {
    mockClientWidth = 640;
    const start = 1_725_000_000;
    const { container } = render(TrendChart, {
      props: {
        zoom: 200, // deep zoom over a ~1h span -> 30-min ticks
        series: [
          {
            name: "Corvette Z06",
            points: [
              { x: start, label: "s1", value: 216.5 },
              { x: start + 1800, label: "s2", value: 215.0 },
              { x: start + 3600, label: "s3", value: 214.2 },
            ],
          },
        ],
      },
    });

    const labels = [...container.querySelectorAll("text.xlabel")].map((t) => t.textContent ?? "");
    expect(labels.some((l) => /\d{1,2}:\d{2}/.test(l))).toBe(true);
  });

  it("collapses same-day sessions to one dot (daily best) when zoomed out", () => {
    mockClientWidth = 640;
    const day = 86400;
    const start = 1_725_000_000;
    const { container } = render(TrendChart, {
      props: {
        zoom: 1, // zoomed out over a multi-day span -> a day is only a few px wide
        series: [
          {
            name: "Corvette Z06",
            points: [
              // day 1: three sessions, best is 214.2
              { x: start + 3600, label: "d1", value: 216.5 },
              { x: start + 7200, label: "d1", value: 214.2 },
              { x: start + 10800, label: "d1", value: 215.0 },
              // days 2 and 3: one each, keeps the span wide (small px/day)
              { x: start + 30 * day, label: "d2", value: 213.0 },
              { x: start + 60 * day, label: "d3", value: 212.4 },
            ],
          },
        ],
      },
    });
    // 3 days -> 3 dots (day 1's three sessions converged to its best).
    expect(container.querySelectorAll("circle").length).toBe(3);
  });

  it("renders a legend entry per car when more than one series", () => {
    const { container } = render(TrendChart, {
      props: {
        series: [
          { name: "Corvette Z06", points: [{ x: 1, label: "9/1", value: 216.5 }] },
          { name: "Ferrari 296", points: [{ x: 1, label: "9/1", value: 220.1 }] },
        ],
      },
    });
    expect(container.querySelectorAll(".legend li").length).toBe(2);
  });

  it("shows a placeholder when there is less than one point", () => {
    const { container, getByText } = render(TrendChart, { props: { series: [] } });

    expect(container.querySelector("svg")).toBeNull();
    expect(getByText(/no laps recorded here yet/i)).toBeTruthy();
  });

  it("shows a custom placeholder message when provided", () => {
    const { getByText } = render(TrendChart, {
      props: { series: [], placeholder: "Select a track to see the trend" },
    });

    expect(getByText(/select a track to see the trend/i)).toBeTruthy();
  });
});
