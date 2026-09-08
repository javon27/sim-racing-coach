import { describe, it, expect } from "vitest";
import { zoomAt, type Box } from "./mapzoom";

// Fake SVG whose CTM is identity, so client coords == viewBox coords — lets us
// test the zoom math without a real SVG.
const fakeSvg = {
  getScreenCTM: () => ({ inverse: () => ({}) }),
  createSVGPoint: () => ({
    x: 0,
    y: 0,
    matrixTransform() {
      return { x: this.x, y: this.y };
    },
  }),
} as unknown as SVGSVGElement;

const full: Box = { x: 0, y: 0, w: 100, h: 100 };

describe("zoomAt", () => {
  it("zooms in toward the cursor, keeping that point fixed", () => {
    const v = zoomAt(full, full, 25, 25, -1, fakeSvg); // scroll up at (25,25)
    expect(v.w).toBeLessThan(100);
    // the cursor point must map to the same fraction of the new box
    expect((25 - v.x) / v.w).toBeCloseTo(0.25, 5);
    expect((25 - v.y) / v.h).toBeCloseTo(0.25, 5);
  });

  it("never zooms out past the full box", () => {
    const v = zoomAt(full, full, 50, 50, 1, fakeSvg); // scroll down from full
    expect(v).toEqual(full);
  });

  it("clamps the box inside the full extent", () => {
    let v: Box = full;
    for (let i = 0; i < 20; i++) v = zoomAt(v, full, 100, 100, -1, fakeSvg); // zoom into a corner
    expect(v.x + v.w).toBeLessThanOrEqual(100 + 1e-6);
    expect(v.y + v.h).toBeLessThanOrEqual(100 + 1e-6);
    expect(v.x).toBeGreaterThanOrEqual(-1e-6);
  });
});
