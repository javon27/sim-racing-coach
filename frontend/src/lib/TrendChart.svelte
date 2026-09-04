<script lang="ts">
  // Best-lap trend, one line per car. Each point is its own session (keyed by a
  // sortable `x`, e.g. the session start timestamp) so same-day sessions spread
  // across the axis instead of stacking on one date. Horizontally scrollable with
  // zoom (buttons + Ctrl/Cmd+wheel); the plot width grows with the zoom level.

  type Point = { x: number; label: string; value: number };
  type Series = { name: string; color?: string; points: Point[] };

  let {
    series = [],
    placeholder = "No laps recorded here yet",
    gap = $bindable(64), // px between adjacent session slots (the zoom level)
  }: {
    series?: Series[];
    placeholder?: string;
    gap?: number;
  } = $props();

  const PALETTE = ["#6d4aff", "#0e9f6e", "#da2e2e", "#b45c09", "#0aa1c5"];
  const MIN_GAP = 20;
  const MAX_GAP = 200;

  function formatLap(seconds: number): string {
    const m = Math.floor(seconds / 60);
    const s = (seconds - m * 60).toFixed(3);
    return `${m}:${s.padStart(6, "0")}`;
  }

  const totalPoints = $derived(series.reduce((n, s) => n + s.points.length, 0));

  function setGap(next: number) {
    gap = Math.max(MIN_GAP, Math.min(MAX_GAP, next));
  }
  function zoomIn() {
    setGap(Math.round(gap * 1.25));
  }
  function zoomOut() {
    setGap(Math.round(gap / 1.25));
  }
  function onWheel(e: WheelEvent) {
    // Ctrl/Cmd+wheel zooms; a plain wheel scrolls the container normally.
    if (!e.ctrlKey && !e.metaKey) return;
    e.preventDefault();
    setGap(gap * (e.deltaY < 0 ? 1.1 : 1 / 1.1));
  }

  const chart = $derived.by(() => {
    const H = 250, L = 64, R = 18, T = 16, B = 34;
    const plotH = H - T - B;

    const allY = series.flatMap((s) => s.points.map((p) => p.value));
    const mn = Math.min(...allY);
    const mx = Math.max(...allY);
    const rng = mx - mn || 1;

    // Shared ordered axis of distinct session x-keys across all series.
    const slots = [...new Set(series.flatMap((s) => s.points.map((p) => p.x)))].sort(
      (a, b) => a - b,
    );
    const slotIndex = new Map(slots.map((x, i) => [x, i]));
    // A label for each slot (date string), taken from any series' point at that x.
    const labelFor = new Map<number, string>();
    for (const s of series) for (const p of s.points) labelFor.set(p.x, p.label);

    const g = Math.max(MIN_GAP, Math.min(MAX_GAP, gap));
    const plotW = Math.max(1, slots.length - 1) * g;
    const W = L + plotW + R;

    const x = (xkey: number) =>
      L + (slots.length < 2 ? plotW / 2 : (slotIndex.get(xkey) as number) * g);
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

    // Thin x-labels so they stay ~≥52px apart at the current zoom.
    const step = Math.max(1, Math.ceil(52 / g));
    const xLabels = slots
      .map((xkey, i) => ({ x: x(xkey), label: labelFor.get(xkey) ?? "", i }))
      .filter((l) => l.i % step === 0);

    return { W, H, L, R, gridLines, lines, xLabels };
  });
</script>

{#if totalPoints < 1}
  <div class="chart-empty">{placeholder}</div>
{:else}
  <div class="chart-wrap">
    <div class="zoom" role="group" aria-label="Zoom">
      <button type="button" onclick={zoomOut} aria-label="Zoom out" disabled={gap <= MIN_GAP}>−</button>
      <button type="button" onclick={zoomIn} aria-label="Zoom in" disabled={gap >= MAX_GAP}>+</button>
    </div>
    <div class="chart-scroll" onwheel={onWheel}>
      <svg viewBox="0 0 {chart.W} {chart.H}" width={chart.W} height={chart.H} role="img" aria-label="Best lap trend">
        {#each chart.gridLines as gl}
          <line x1={chart.L} y1={gl.y} x2={chart.W - chart.R} y2={gl.y} class="grid" />
          <text x={chart.L - 8} y={gl.y + 3} text-anchor="end" class="ylabel">{gl.label}</text>
        {/each}
        {#each chart.lines as line (line.name)}
          <path d={line.path} fill="none" stroke={line.color} stroke-width="2.5" stroke-linejoin="round" stroke-linecap="round" />
          {#each line.dots as d}
            <circle cx={d[0].toFixed(1)} cy={d[1].toFixed(1)} r="3.5" fill={line.color} class="dot" />
          {/each}
        {/each}
        {#each chart.xLabels as xl (xl.i)}
          <text x={xl.x} y={chart.H - 12} text-anchor="middle" class="xlabel">{xl.label}</text>
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
