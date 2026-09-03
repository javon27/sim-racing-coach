import { describe, it, expect, afterEach } from "vitest";
import { render, cleanup } from "@testing-library/svelte";
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
              { date: "9/1", value: 216.5 },
              { date: "9/2", value: 214.0 },
              { date: "9/3", value: 213.2 },
            ],
          },
        ],
      },
    });

    const svg = container.querySelector("svg");
    expect(svg).toBeTruthy();
    expect(container.querySelectorAll("circle").length).toBe(3);
  });

  it("renders one path per series for multiple cars", () => {
    const { container } = render(TrendChart, {
      props: {
        series: [
          { name: "Corvette Z06", points: [{ date: "9/1", value: 216.5 }, { date: "9/2", value: 214.0 }] },
          { name: "Ferrari 296", points: [{ date: "9/1", value: 220.1 }, { date: "9/2", value: 218.0 }] },
        ],
      },
    });

    expect(container.querySelectorAll("path").length).toBe(2);
    expect(container.querySelectorAll("circle").length).toBe(4);
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
