// Scroll-wheel zoom for the SVG track maps (read-only view + editor). Zooming by
// shrinking the viewBox toward the cursor keeps every existing pointer→world
// mapping (svgEl.getScreenCTM) correct with no other changes — placing corners
// while zoomed in stays accurate.

export interface Box {
  x: number;
  y: number;
  w: number;
  h: number;
}

const MAX_ZOOM = 12; // smallest viewBox = full / MAX_ZOOM
const STEP = 1.15; // per wheel notch

const clamp = (v: number, lo: number, hi: number) => Math.max(lo, Math.min(v, hi));

// Cursor client point -> current-viewBox coords, via the SVG's own CTM so it
// honors the live viewBox AND preserveAspectRatio letterboxing.
function cursorInBox(svgEl: SVGSVGElement, clientX: number, clientY: number): [number, number] | null {
  const ctm = svgEl.getScreenCTM?.();
  if (!ctm) return null;
  const pt = svgEl.createSVGPoint();
  pt.x = clientX;
  pt.y = clientY;
  const v = pt.matrixTransform(ctm.inverse());
  return [v.x, v.y];
}

// New viewBox after one wheel event. deltaY < 0 (scroll up) zooms in. The point
// under the cursor stays put; the box is clamped to `full` so you never pan off
// into empty space, and scrolling all the way out snaps back to the full map.
export function zoomAt(
  view: Box,
  full: Box,
  clientX: number,
  clientY: number,
  deltaY: number,
  svgEl: SVGSVGElement,
): Box {
  const c = cursorInBox(svgEl, clientX, clientY);
  if (!c) return view;
  const [cx, cy] = c;
  const desired = view.w * (deltaY < 0 ? 1 / STEP : STEP);
  const w = clamp(desired, full.w / MAX_ZOOM, full.w);
  const k = w / view.w; // actual scale applied (after clamping)
  const h = view.h * k;
  const nx = clamp(cx - (cx - view.x) * k, full.x, full.x + full.w - w);
  const ny = clamp(cy - (cy - view.y) * k, full.y, full.y + full.h - h);
  return { x: nx, y: ny, w, h };
}

// Wire wheel zoom onto an svg element: keeps `get`/`set` view state in sync and
// swallows the page scroll. Returns a teardown. Uses a non-passive listener so
// preventDefault actually stops the page from scrolling under the map.
export function attachWheelZoom(
  svgEl: SVGSVGElement,
  get: () => { view: Box; full: Box },
  set: (v: Box) => void,
): () => void {
  const onWheel = (e: WheelEvent) => {
    e.preventDefault();
    const { view, full } = get();
    set(zoomAt(view, full, e.clientX, e.clientY, e.deltaY, svgEl));
  };
  svgEl.addEventListener("wheel", onWheel, { passive: false });
  return () => svgEl.removeEventListener("wheel", onWheel);
}

// Drag-to-pan while zoomed in. `canStart(e)` lets the editor veto (add mode, or a
// pointerdown on an interactive marker) so placing/dragging still works. Panning
// is disabled at full zoom and always clamped inside `full`.
export function attachPan(
  svgEl: SVGSVGElement,
  get: () => { view: Box; full: Box },
  set: (v: Box) => void,
  canStart: (e: PointerEvent) => boolean,
): () => void {
  let start: { cx: number; cy: number; view: Box; full: Box } | null = null;

  const onDown = (e: PointerEvent) => {
    const { view, full } = get();
    if (view.w >= full.w || !canStart(e)) return; // not zoomed, or vetoed
    start = { cx: e.clientX, cy: e.clientY, view, full };
    svgEl.setPointerCapture?.(e.pointerId);
    svgEl.style.cursor = "grabbing";
  };
  const onMove = (e: PointerEvent) => {
    if (!start) return;
    const rect = svgEl.getBoundingClientRect();
    if (!rect.width || !rect.height) return;
    const { view, full } = start;
    const dx = ((e.clientX - start.cx) * view.w) / rect.width;
    const dy = ((e.clientY - start.cy) * view.h) / rect.height;
    set({
      ...view,
      x: clamp(view.x - dx, full.x, full.x + full.w - view.w),
      y: clamp(view.y - dy, full.y, full.y + full.h - view.h),
    });
  };
  const onUp = (e: PointerEvent) => {
    start = null;
    svgEl.releasePointerCapture?.(e.pointerId);
    svgEl.style.cursor = "";
  };

  svgEl.addEventListener("pointerdown", onDown);
  svgEl.addEventListener("pointermove", onMove);
  svgEl.addEventListener("pointerup", onUp);
  svgEl.addEventListener("pointercancel", onUp);
  return () => {
    svgEl.removeEventListener("pointerdown", onDown);
    svgEl.removeEventListener("pointermove", onMove);
    svgEl.removeEventListener("pointerup", onUp);
    svgEl.removeEventListener("pointercancel", onUp);
  };
}
