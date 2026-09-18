<script module lang="ts">
  // Zoom range/step shared with the parent, which renders the zoom buttons in the
  // card header (same row as the title). clampZoom keeps both sides in range.
  export const ZOOM_MIN = 1;
  export const ZOOM_MAX = 400;
  export const ZOOM_STEP = 1.5;
  export const clampZoom = (z: number) => Math.max(ZOOM_MIN, Math.min(ZOOM_MAX, z));
</script>

<script lang="ts">
  import { untrack } from "svelte";
  import { Tween, prefersReducedMotion } from "svelte/motion";
  import { cubicOut } from "svelte/easing";

  // Best-lap trend, one line per car (color per series). Each point is a session
  // keyed by a unix-seconds `x`, placed by its ACTUAL time. At base zoom the plot
  // fills the container; zooming in grows it wider (horizontal scroll).
  //
  // - Y-axis is a fixed left column: it never scrolls horizontally, and its scale
  //   auto-fits the points currently VISIBLE in the scroll viewport.
  // - Same-day sessions collapse to that day's BEST lap while zoomed out; zooming
  //   in past DAY_COLLAPSE_PX (a day wide enough to separate sessions) shows the
  //   individual session times.
  // - Zoom: buttons live in the parent's card header (bind:zoom) + Ctrl/Cmd+wheel here.

  type Point = { x: number; label: string; value: number };
  type Series = { name: string; color?: string; points: Point[] };

  let {
    series = [],
    placeholder = "No laps recorded here yet",
    zoom = $bindable(1), // plot-width multiplier over the base (container-fitting) width
  }: {
    series?: Series[];
    placeholder?: string;
    zoom?: number;
  } = $props();

  const PALETTE = ["#6d4aff", "#0e9f6e", "#da2e2e", "#b45c09", "#0aa1c5"];
  const MIN_ZOOM = ZOOM_MIN;
  const MAX_ZOOM = ZOOM_MAX;
  const MIN_LABEL_PX = 52;
  const FALLBACK_W = 700; // used before the container reports its width
  const AXIS_W = 58; // fixed y-axis gutter (px)
  const DAY_COLLAPSE_PX = 160; // below this per-day width, collapse same-day → daily best
  // Nice tick ladder (seconds): 30m is the finest granularity we ever show.
  const TICK_LADDER = [
    1800, 3600, 3 * 3600, 6 * 3600, 12 * 3600,
    86400, 2 * 86400, 7 * 86400, 14 * 86400, 30 * 86400, 90 * 86400, 365 * 86400,
  ];

  let containerEl: HTMLDivElement | undefined = $state();
  let containerWidth = $state(0);
  let scrollLeft = $state(0);

  // Track the scroll container's width so the base plot fills it. ResizeObserver
  // when available (real browser); a one-shot read otherwise (jsdom/SSR).
  $effect(() => {
    const el = containerEl;
    if (!el) return;
    const measure = () => (containerWidth = el.clientWidth);
    measure();
    if (typeof ResizeObserver === "undefined") return;
    const ro = new ResizeObserver(measure);
    ro.observe(el);
    return () => ro.disconnect();
  });

  function formatLap(seconds: number): string {
    const m = Math.floor(seconds / 60);
    const s = (seconds - m * 60).toFixed(3);
    return `${m}:${s.padStart(6, "0")}`;
  }

  function dayKey(x: number): string {
    const d = new Date(x * 1000);
    return `${d.getFullYear()}-${d.getMonth()}-${d.getDate()}`;
  }

  const totalPoints = $derived(series.reduce((n, s) => n + s.points.length, 0));

  function onWheel(e: WheelEvent) {
    // Ctrl/Cmd+wheel zooms; a plain wheel scrolls the container normally.
    if (!e.ctrlKey && !e.metaKey) return;
    e.preventDefault();
    zoom = clampZoom(zoom * (e.deltaY < 0 ? 1.25 : 1 / 1.25));
  }
  function onScroll(e: Event) {
    scrollLeft = (e.currentTarget as HTMLDivElement).scrollLeft;
  }

  function formatTick(t: number, iv: number, prevDay: string): string {
    const d = new Date(t * 1000);
    const date = d.toLocaleDateString(undefined, { month: "numeric", day: "numeric" });
    if (iv >= 86400) return date;
    const hhmm = d.toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit", hour12: false });
    // Within the same day as the previous tick, drop the redundant date.
    return d.toDateString() === prevDay ? hhmm : `${date} ${hhmm}`;
  }

  const legend = $derived(
    series.map((s, i) => ({ name: s.name, color: s.color ?? PALETTE[i % PALETTE.length] })),
  );

  // Zoom and the y-axis domain are ANIMATED: buttons/scroll/data set a target and
  // these tweens ease toward it, so the whole chart (recomputed from them) glides
  // instead of snapping. `geom` reads the live zoom; `chart` reads the live domain.
  const ANIM_MS = 240;
  const animDur = () => (prefersReducedMotion.current ? 0 : ANIM_MS);

  function domainOf(values: number[]): { mn: number; mx: number } {
    if (!values.length) return { mn: 0, mx: 1 };
    let mn = Math.min(...values);
    let mx = Math.max(...values);
    const pad = (mx - mn) * 0.08 || Math.max(0.5, mx * 0.001);
    return { mn: mn - pad, mx: mx + pad };
  }

  // Initial tween values snapshot the props at mount (untrack: intentional, not reactive).
  const init = untrack(() => domainOf(series.flatMap((s) => s.points.map((p) => p.value))));
  const zoomTween = new Tween(untrack(() => Math.max(MIN_ZOOM, Math.min(MAX_ZOOM, zoom))), { easing: cubicOut });
  const mnTween = new Tween(init.mn, { easing: cubicOut });
  const mxTween = new Tween(init.mx, { easing: cubicOut });

  $effect(() => {
    zoomTween.set(Math.max(MIN_ZOOM, Math.min(MAX_ZOOM, zoom)), { duration: animDur() });
  });
  $effect(() => {
    const d = targetDomain;
    mnTween.set(d.mn, { duration: animDur() });
    mxTween.set(d.mx, { duration: animDur() });
  });

  // Geometry (x-axis, widths, per-point pixel-x) — driven by the animating zoom.
  const geom = $derived.by(() => {
    const H = 250, LPAD = 10, R = 18, T = 16, B = 34;
    const plotH = H - T - B;
    const plotTop = T;
    const plotBottom = H - B;

    const allX = series.flatMap((s) => s.points.map((p) => p.x));
    const minX = Math.min(...allX);
    const maxX = Math.max(...allX);
    const span = maxX - minX; // seconds

    // Base plot width fits the span into the container; zoom scales from there.
    const cw = containerWidth || FALLBACK_W;
    const basePlotW = Math.max(1, cw - LPAD - R);
    const z = Math.max(MIN_ZOOM, Math.min(MAX_ZOOM, zoomTween.current));
    const plotW = basePlotW * z;
    const W = LPAD + plotW + R;
    const pxPerSec = span > 0 ? plotW / span : 0;

    // Time -> px. All points sharing one timestamp are centered.
    const x = (t: number) => (span > 0 ? LPAD + (t - minX) * pxPerSec : LPAD + plotW / 2);

    // Collapse same-day sessions to that day's best while a day is too narrow to
    // separate its sessions; zoom in past the threshold to see them individually.
    const collapse = pxPerSec * 86400 < DAY_COLLAPSE_PX;
    const prepared = series.map((s, i) => {
      const color = s.color ?? PALETTE[i % PALETTE.length];
      let pts = s.points;
      if (collapse) {
        const byDay = new Map<string, Point>();
        for (const p of s.points) {
          const cur = byDay.get(dayKey(p.x));
          if (!cur || p.value < cur.value) byDay.set(dayKey(p.x), p);
        }
        pts = [...byDay.values()];
      }
      pts = [...pts].sort((a, b) => a.x - b.x);
      const px = pts.map((p) => ({ px: x(p.x), value: p.value }));
      return { name: s.name, color, pts, px };
    });

    // Adaptive x-axis ticks: smallest ladder interval whose pixel spacing ≥ ~52px.
    let iv = TICK_LADDER[TICK_LADDER.length - 1];
    for (const c of TICK_LADDER) {
      if (c * pxPerSec >= MIN_LABEL_PX) { iv = c; break; }
    }
    // ponytail: ticks align to epoch multiples of `iv` (30m/1h land on local :00/:30
    // for whole/half-hour zones; day+ ticks anchor to UTC midnight). Good enough for
    // reference gridlines; switch to local-midnight anchoring if a zone looks off.
    const ticks: { x: number; label: string }[] = [];
    if (pxPerSec > 0) {
      let prevDay = "";
      for (let t = Math.ceil(minX / iv) * iv; t <= maxX + 0.5; t += iv) {
        ticks.push({ x: x(t), label: formatTick(t, iv, prevDay) });
        prevDay = new Date(t * 1000).toDateString();
      }
    } else {
      // Single timestamp: one centered date label.
      ticks.push({ x: x(minX), label: new Date(minX * 1000).toLocaleDateString(undefined, { month: "numeric", day: "numeric" }) });
    }

    return { H, R, T, plotH, plotTop, plotBottom, W, x, prepared, ticks };
  });

  // Target y-domain: fits the points inside the visible viewport (fallback: all).
  const targetDomain = $derived.by(() => {
    const g = geom;
    const vLeft = scrollLeft;
    const vRight = scrollLeft + (containerWidth || g.W);
    const visible: number[] = [];
    const all: number[] = [];
    for (const s of g.prepared) {
      for (const p of s.px) {
        all.push(p.value);
        if (p.px >= vLeft - 1 && p.px <= vRight + 1) visible.push(p.value);
      }
    }
    return domainOf(visible.length ? visible : all);
  });

  const chart = $derived.by(() => {
    const g = geom;
    const mn = mnTween.current;
    const mx = mxTween.current;
    const rng = mx - mn || 1;
    const y = (v: number) => g.T + (1 - (v - mn) / rng) * g.plotH;

    const gridLines = Array.from({ length: 4 }, (_, gi) => {
      const yy = g.T + (gi / 3) * g.plotH;
      return { y: yy, label: formatLap(mx - (gi / 3) * rng) };
    });

    const lines = g.prepared.map((s) => {
      const dots = s.pts.map((p) => ({ x: g.x(p.x), y: y(p.value), value: p.value, label: p.label }));
      const path = dots.map((p, i2) => `${i2 ? "L" : "M"}${p.x.toFixed(1)} ${p.y.toFixed(1)}`).join(" ");
      return { name: s.name, color: s.color, path, dots };
    });

    return { W: g.W, H: g.H, R: g.R, plotTop: g.plotTop, plotBottom: g.plotBottom, gridLines, lines, ticks: g.ticks };
  });
</script>

{#if totalPoints < 1}
  <div class="chart-empty">{placeholder}</div>
{:else}
  <div class="chart-wrap">
    <div class="plot-row">
      <div class="chart-scroll" bind:this={containerEl} onwheel={onWheel} onscroll={onScroll}>
        <svg viewBox="0 0 {chart.W} {chart.H}" width={chart.W} height={chart.H} role="img" aria-label="Best lap trend">
          {#each chart.gridLines as gl}
            <line x1="0" y1={gl.y} x2={chart.W} y2={gl.y} class="grid" />
          {/each}
          {#each chart.ticks as tk}
            <line x1={tk.x} y1={chart.plotTop} x2={tk.x} y2={chart.plotBottom} class="grid xgrid" />
          {/each}
          {#each chart.lines as line (line.name)}
            <path d={line.path} fill="none" stroke={line.color} stroke-width="2.5" stroke-linejoin="round" stroke-linecap="round" />
            {#each line.dots as d}
              <!-- stroke-transparent widens the hit area for hover without changing the
                   visible dot size or the rendered dot count (existing tests assert it). -->
              <circle cx={d.x.toFixed(1)} cy={d.y.toFixed(1)} r="3.5" fill={line.color} stroke="transparent" stroke-width="14" class="dot">
                <title>{formatLap(d.value)} — {d.label} — {line.name}</title>
              </circle>
            {/each}
          {/each}
          {#each chart.ticks as tk}
            <text x={tk.x} y={chart.H - 12} text-anchor="middle" class="xlabel">{tk.label}</text>
          {/each}
        </svg>
      </div>

      <div class="yaxis" aria-hidden="true">
        <svg viewBox="0 0 {AXIS_W} {chart.H}" width={AXIS_W} height={chart.H}>
          {#each chart.gridLines as gl}
            <text x={AXIS_W - 8} y={gl.y + 3} text-anchor="end" class="ylabel">{gl.label}</text>
          {/each}
        </svg>
      </div>
    </div>

    {#if legend.length > 1}
      <ul class="legend">
        {#each legend as item (item.name)}
          <li><span class="swatch" style="background:{item.color}"></span>{item.name}</li>
        {/each}
      </ul>
    {/if}
  </div>
{/if}

<style>
  .chart-empty {
    height: 250px;
    display: grid;
    place-items: center;
    color: var(--faint);
    font-family: var(--mono);
    font-size: 12px;
    text-align: center;
    border: 1px dashed var(--line);
    border-radius: 10px;
  }
  .plot-row {
    display: flex;
    align-items: stretch;
  }
  /* Plot is DOM-first (tests query the first <svg>); `order` renders it right of
     the fixed y-axis column so the axis never scrolls with the plot. */
  .chart-scroll {
    order: 1;
    flex: 1 1 auto;
    min-width: 0;
    overflow-x: auto;
    overflow-y: hidden;
  }
  .yaxis {
    order: 0;
    flex: 0 0 auto;
    width: 58px;
  }
  .grid {
    stroke: var(--line);
  }
  .xgrid {
    opacity: 0.5;
  }
  .ylabel,
  .xlabel {
    fill: var(--faint);
    font-family: var(--mono);
    font-size: 10px;
  }
  .dot {
    stroke: var(--surface);
    stroke-width: 1.5;
  }
  .legend {
    display: flex;
    flex-wrap: wrap;
    gap: 6px 16px;
    margin: 10px 0 0;
    padding: 0 0 0 58px;
    list-style: none;
  }
  .legend li {
    display: flex;
    align-items: center;
    gap: 6px;
    font-size: 11px;
    color: var(--muted);
  }
  .swatch {
    width: 10px;
    height: 10px;
    border-radius: 2px;
    flex: 0 0 auto;
  }
</style>
