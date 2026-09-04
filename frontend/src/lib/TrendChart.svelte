<script lang="ts">
  // Best-lap trend, one line per car. Each point is its own session keyed by a
  // unix-seconds `x`, and points are placed by their ACTUAL time (proportional
  // to `x`), so same-day sessions separate by time-of-day. At base zoom the plot
  // fills the container width; zooming in grows it wider (horizontal scroll).
  // X-axis gridlines are drawn at "nice" time intervals (down to 30 min) chosen
  // from the current time-per-pixel. Zoom: ± buttons + Ctrl/Cmd+wheel.

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
  // Nice tick ladder (seconds): 30m is the finest granularity we ever show.
  const TICK_LADDER = [
    1800, 3600, 3 * 3600, 6 * 3600, 12 * 3600,
    86400, 2 * 86400, 7 * 86400, 14 * 86400, 30 * 86400, 90 * 86400, 365 * 86400,
  ];

  let containerEl: HTMLDivElement | undefined = $state();
  let containerWidth = $state(0);

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

  function formatTick(t: number, iv: number, prevDay: string): string {
    const d = new Date(t * 1000);
    const date = d.toLocaleDateString(undefined, { month: "numeric", day: "numeric" });
    if (iv >= 86400) return date;
    const hhmm = d.toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit", hour12: false });
    // Within the same day as the previous tick, drop the redundant date.
    return d.toDateString() === prevDay ? hhmm : `${date} ${hhmm}`;
  }

  const chart = $derived.by(() => {
    const H = 250, L = 64, R = 18, T = 16, B = 34;
    const plotH = H - T - B;
    const plotTop = T;
    const plotBottom = H - B;

    const allY = series.flatMap((s) => s.points.map((p) => p.value));
    const mn = Math.min(...allY);
    const mx = Math.max(...allY);
    const rng = mx - mn || 1;

    const allX = series.flatMap((s) => s.points.map((p) => p.x));
    const minX = Math.min(...allX);
    const maxX = Math.max(...allX);
    const span = maxX - minX; // seconds

    // Base plot width fits the span into the container; zoom scales from there.
    const cw = containerWidth || FALLBACK_W;
    const basePlotW = Math.max(1, cw - L - R);
    const z = Math.max(MIN_ZOOM, Math.min(MAX_ZOOM, zoom));
    const plotW = basePlotW * z;
    const W = L + plotW + R;
    const pxPerSec = span > 0 ? plotW / span : 0;

    // Time -> px. All points sharing one timestamp are centered.
    const x = (t: number) => (span > 0 ? L + (t - minX) * pxPerSec : L + plotW / 2);
    const y = (v: number) => T + (1 - (v - mn) / rng) * plotH;

    const gridLines = Array.from({ length: 4 }, (_, gi) => {
      const yy = T + (gi / 3) * plotH;
      const val = mx - (gi / 3) * rng;
      return { y: yy, label: formatLap(val) };
    });

    const lines = series.map((s, i) => {
      const color = s.color ?? PALETTE[i % PALETTE.length];
      const pts = [...s.points]
        .sort((a, b) => a.x - b.x)
        .map((p) => [x(p.x), y(p.value)] as const);
      const path = pts.map((p, i2) => `${i2 ? "L" : "M"}${p[0].toFixed(1)} ${p[1].toFixed(1)}`).join(" ");
      return { name: s.name, color, path, dots: pts };
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

    return { W, H, L, R, plotTop, plotBottom, gridLines, lines, ticks };
  });
</script>

{#if totalPoints < 1}
  <div class="chart-empty">{placeholder}</div>
{:else}
  <div class="chart-wrap">
    <div class="zoom" role="group" aria-label="Zoom">
      <button type="button" onclick={zoomOut} aria-label="Zoom out" disabled={zoom <= MIN_ZOOM}>−</button>
      <button type="button" onclick={zoomIn} aria-label="Zoom in" disabled={zoom >= MAX_ZOOM}>+</button>
    </div>
    <div class="chart-scroll" bind:this={containerEl} onwheel={onWheel}>
      <svg viewBox="0 0 {chart.W} {chart.H}" width={chart.W} height={chart.H} role="img" aria-label="Best lap trend">
        {#each chart.gridLines as gl}
          <line x1={chart.L} y1={gl.y} x2={chart.W - chart.R} y2={gl.y} class="grid" />
          <text x={chart.L - 8} y={gl.y + 3} text-anchor="end" class="ylabel">{gl.label}</text>
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
  .chart-wrap {
    position: relative;
  }
  .chart-scroll {
    overflow-x: auto;
    overflow-y: hidden;
  }
  .zoom {
    position: absolute;
    top: 4px;
    right: 6px;
    z-index: 1;
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
</style>
