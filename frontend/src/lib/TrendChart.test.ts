import { describe, it, expect, afterEach } from "vitest";
import { render, cleanup, fireEvent } from "@testing-library/svelte";
import TrendChart from "./TrendChart.svelte";

describe("TrendChart", () => {
  afterEach(cleanup);

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
    // All three render at DIFFERENT x — they no longer collapse onto one date slot.
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

  it("zoom-in widens the plot (more px per slot)", async () => {
    const series = [
      { name: "Corvette Z06", points: [{ x: 1, label: "9/1", value: 216.5 }, { x: 2, label: "9/2", value: 214.0 }] },
    ];
    const { container, getByLabelText } = render(TrendChart, { props: { series, gap: 64 } });
    const before = Number(container.querySelector("svg")!.getAttribute("width"));
    await fireEvent.click(getByLabelText("Zoom in"));
    const after = Number(container.querySelector("svg")!.getAttribute("width"));
    expect(after).toBeGreaterThan(before);
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
