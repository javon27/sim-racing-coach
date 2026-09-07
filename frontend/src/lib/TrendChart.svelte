<script lang="ts">
  // Best-lap trend, one line per car (color per series). Each point is a session
  // keyed by a unix-seconds `x`, placed by its ACTUAL time. At base zoom the plot
  // fills the container; zooming in grows it wider (horizontal scroll).
  //
  // - Y-axis is a fixed left column: it never scrolls horizontally, and its scale
  //   auto-fits the points currently VISIBLE in the scroll viewport.
  // - Same-day sessions collapse to that day's BEST lap while zoomed out; zooming
  //   in past DAY_COLLAPSE_PX (a day wide enough to separate sessions) shows the
  //   individual session times.
  // - Zoom: toolbar buttons above the plot + Ctrl/Cmd+wheel.

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
  const MIN_ZOOM = 1;
  const MAX_ZOOM = 400;
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

  function setZoom(next: number) {
    zoom = Math.max(MIN_ZOOM, Math.min(MAX_ZOOM, next));
  }
  function zoomIn() {
    setZoom(zoom * 1.5);
  }
  function zoomOut() {
    setZoom(zoom / 1.5);
  }
  function onWheel(e: WheelEvent) {
    // Ctrl/Cmd+wheel zooms; a plain wheel scrolls the container normally.
    if (!e.ctrlKey && !e.metaKey) return;
    e.preventDefault();
    setZoom(zoom * (e.deltaY < 0 ? 1.25 : 1 / 1.25));
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

  const chart = $derived.by(() => {
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
    const z = Math.max(MIN_ZOOM, Math.min(MAX_ZOOM, zoom));
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

    // Y scale fits the points inside the visible viewport (fallback: all points).
    const vLeft = scrollLeft;
    const vRight = scrollLeft + (containerWidth || W);
    const visibleY: number[] = [];
    const allY: number[] = [];
    for (const s of prepared) {
      for (const p of s.px) {
        allY.push(p.value);
        if (p.px >= vLeft - 1 && p.px <= vRight + 1) visibleY.push(p.value);
      }
    }
    const domainY = visibleY.length ? visibleY : allY;
    let mn = Math.min(...domainY);
    let mx = Math.max(...domainY);
    const pad = (mx - mn) * 0.08 || Math.max(0.5, mx * 0.001);
    mn -= pad;
    mx += pad;
    const rng = mx - mn || 1;
    const y = (v: number) => T + (1 - (v - mn) / rng) * plotH;

    const gridLines = Array.from({ length: 4 }, (_, gi) => {
      const yy = T + (gi / 3) * plotH;
      return { y: yy, label: formatLap(mx - (gi / 3) * rng) };
    });

    const lines = prepared.map((s) => {
      const dots = s.pts.map((p) => [x(p.x), y(p.value)] as const);
      const path = dots.map((p, i2) => `${i2 ? "L" : "M"}${p[0].toFixed(1)} ${p[1].toFixed(1)}`).join(" ");
      return { name: s.name, color: s.color, path, dots };
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

    return { W, H, R, plotTop, plotBottom, gridLines, lines, ticks };
  });
</script>

{#if totalPoints < 1}
  <div class="chart-empty">{placeholder}</div>
{:else}
  <div class="chart-wrap">
    <div class="toolbar">
      <div class="zoom" role="group" aria-label="Zoom">
        <button type="button" onclick={zoomOut} aria-label="Zoom out" disabled={zoom <= MIN_ZOOM}>−</button>
        <button type="button" onclick={zoomIn} aria-label="Zoom in" disabled={zoom >= MAX_ZOOM}>+</button>
      </div>
    </div>

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
              <circle cx={d[0].toFixed(1)} cy={d[1].toFixed(1)} r="3.5" fill={line.color} class="dot" />
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
  .toolbar {
    display: flex;
    justify-content: flex-end;
    margin-bottom: 6px;
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
  .zoom {
    display: flex;
    gap: 4px;
  }
  .zoom button {
    width: 24px;
    height: 24px;
    line-height: 1;
    font-size: 16px;
    font-family: var(--mono);
    color: var(--text);
    background: var(--surface);
    border: 1px solid var(--line);
    border-radius: 6px;
    cursor: pointer;
  }
  .zoom button:disabled {
    opacity: 0.4;
    cursor: default;
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
