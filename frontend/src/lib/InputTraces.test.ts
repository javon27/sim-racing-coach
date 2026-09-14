import { describe, it, expect, afterEach } from "vitest";
import { render, cleanup } from "@testing-library/svelte";
import InputTraces from "./InputTraces.svelte";
import type { InputTrace, InputTraces as Traces } from "./api";

// jsdom does not lay out or paint SVG, so these assert the geometry the component
// COMPUTES — the `d` attribute it writes — not how it looks. Computed styles are
// worthless here (vitest doesn't process app.css, so every CSS var is empty).

const flat = (v: number[]) => ({ lo: [...v], hi: [...v] });

function trace(over: Partial<InputTrace> = {}): InputTrace {
  return {
    lap_id: 7,
    lap_time: 92.5,
    length: 1000,
    dist: [0, 500, 1000],
    throttle: flat([1, 0, 1]),
    brake: flat([0, 1, 0]),
    steering: flat([-1, 0, 1]),
    ...over,
  };
}

// Parse "M0.0 0.0 L500.0 46.0 … Z" -> [[x, y], ...]
function points(d: string): number[][] {
  return d
    .replace(/\s*Z\s*$/, "")
    .trim()
    .split(/(?=[ML])/)
    .map((seg) => seg.replace(/^[ML]/, "").trim().split(/\s+/).map(Number));
}

// The band is drawn hi-edge forward then lo-edge back, so the first half is hi.
function edges(d: string): { hi: number[][]; lo: number[][] } {
  const p = points(d);
  const n = p.length / 2;
  return { hi: p.slice(0, n), lo: p.slice(n).reverse() };
}

describe("InputTraces (Pit Wall driver-input graphs)", () => {
  afterEach(() => cleanup());

  it("prompts for a lap when there is nothing to draw", () => {
    const { getByTestId } = render(InputTraces, { props: { traces: null } });
    expect(getByTestId("inputs-empty")).toBeTruthy();
  });

  it("maps lap distance to x across the full plot width", () => {
    const { getByTestId } = render(InputTraces, { props: { traces: { last: trace(), best: null } } });
    const { hi } = edges(getByTestId("trace-throttle-last").getAttribute("d")!);
    expect(hi.map((p) => p[0])).toEqual([0, 500, 1000]); // 0 / 500 / 1000 m of a 1000 m lap
  });

  it("maps a 0..1 pedal onto the row: 1 at the top edge, 0 at the bottom", () => {
    const { getByTestId } = render(InputTraces, { props: { traces: { last: trace(), best: null } } });
    const thr = edges(getByTestId("trace-throttle-last").getAttribute("d")!);
    // Throttle is the first row (top 0, height 46): full = y 0, released = y 46.
    expect(thr.hi.map((p) => p[1])).toEqual([0, 46, 0]);
    const brk = edges(getByTestId("trace-brake-last").getAttribute("d")!);
    // Brake is the second row (top 46 + 4 gap = 50).
    expect(brk.hi.map((p) => p[1])).toEqual([96, 50, 96]);
  });

  it("centres steering on its own row: full lock either way spans the band", () => {
    const { getByTestId } = render(InputTraces, { props: { traces: { last: trace(), best: null } } });
    const st = edges(getByTestId("trace-steering-last").getAttribute("d")!);
    // Third row top = 2 * (46 + 4) = 100. -1 -> bottom (146), 0 -> middle (123), +1 -> top (100).
    expect(st.hi.map((p) => p[1])).toEqual([146, 123, 100]);
  });

  it("draws BOTH envelope edges, so a sub-tick lift can't vanish", () => {
    // The reason the sidecar sends min/max per bin: a bin is coarser than the
    // game's scoring tick, so the extremes are the signal. A renderer that drew
    // only one edge would throw the lift away again.
    const dip = trace({ throttle: { hi: [1, 1, 1], lo: [1, 0, 1] } });
    const { getByTestId } = render(InputTraces, { props: { traces: { last: dip, best: null } } });
    const { hi, lo } = edges(getByTestId("trace-throttle-last").getAttribute("d")!);
    expect(hi.map((p) => p[1])).toEqual([0, 0, 0]); // held flat at the top
    expect(lo.map((p) => p[1])).toEqual([0, 46, 0]); // the lift, on the lo edge
  });

  it("closes the band path so it fills", () => {
    const { getByTestId } = render(InputTraces, { props: { traces: { last: trace(), best: null } } });
    expect(getByTestId("trace-throttle-last").getAttribute("d")!.trimEnd()).toMatch(/Z$/);
  });

  it("aligns a shorter best lap on NORMALIZED progress, not raw metres", () => {
    // The best lap measured 4 m shorter; both still start at the S/F line, so the
    // two traces must span the same x range (a raw-metre x would leave a gap).
    const best = trace({ lap_time: 90.0, length: 996, dist: [0, 498, 996] });
    const { getByTestId } = render(InputTraces, { props: { traces: { last: trace(), best } } });
    const a = edges(getByTestId("trace-throttle-last").getAttribute("d")!);
    const b = edges(getByTestId("trace-throttle-best").getAttribute("d")!);
    expect(b.hi.map((p) => p[0])).toEqual(a.hi.map((p) => p[0]));
  });

  it("omits the best trace when the server sends none (last lap IS the best)", () => {
    const { queryByTestId, getByTestId } = render(InputTraces, {
      props: { traces: { last: trace(), best: null } as Traces },
    });
    expect(queryByTestId("trace-throttle-best")).toBeNull();
    expect(getByTestId("inputs-last-time").textContent).toBe("1:32.500");
  });
});
