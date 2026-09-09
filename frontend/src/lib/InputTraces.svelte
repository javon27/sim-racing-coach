<!-- frontend/src/lib/InputTraces.svelte
     Driver-input graphs for the Pit Wall (#21): throttle, brake and steering
     against lap distance, last lap overlaid on the reference best.

     DUMB BY DESIGN. The sidecar (GET /track/inputs -> analysis.input_traces) does
     the de-staircasing of the 5 Hz lap_distance channel and the downsample to 500
     points per channel; this file only maps numbers to an SVG path. No resampling,
     no smoothing, no analysis here — that belongs in the core.

     Laps are paired by NORMALIZED lap progress, the same convention ribbon.py uses:
     both laps start at the S/F line, so t = (d - d0) / (dN - d0) lines them up even
     when their measured lengths differ by a few metres. -->
<script lang="ts">
  import type { InputTrace, InputTraces } from "./api";

  let { traces = null }: { traces?: InputTraces | null } = $props();

  const W = 1000;
  const ROW_H = 46;
  const GAP = 4;
  const AXIS_H = 16;

  // [key, label, y-domain]. Steering is signed (full lock either way); the pedals
  // are 0..1, drawn on a fixed domain so a light lap doesn't rescale to look heavy.
  const ROWS = [
    { key: "throttle", label: "Throttle", lo: 0, hi: 1 },
    { key: "brake", label: "Brake", lo: 0, hi: 1 },
    { key: "steering", label: "Steer", lo: -1, hi: 1 },
  ] as const;

  const H = ROWS.length * ROW_H + (ROWS.length - 1) * GAP + AXIS_H;

  function path(tr: InputTrace, key: (typeof ROWS)[number]["key"], top: number,
                lo: number, hi: number): string {
    const d = tr.dist;
    const v = tr[key];
    const n = Math.min(d.length, v.length);
    if (n < 2) return "";
    const d0 = d[0];
    const span = d[n - 1] - d0 || 1;
    const rng = hi - lo || 1;
    let out = "";
    for (let i = 0; i < n; i++) {
      const x = ((d[i] - d0) / span) * W;
      const y = top + (1 - (v[i] - lo) / rng) * ROW_H;
      out += `${i ? "L" : "M"}${x.toFixed(1)} ${y.toFixed(1)}`;
      if (i < n - 1) out += " ";
    }
    return out;
  }

  const rows = $derived(
    ROWS.map((r, i) => {
      const top = i * (ROW_H + GAP);
      return {
        ...r,
        top,
        // Zero line only where it sits inside the plot (steering); pedals rest on the floor.
        zero: r.lo < 0 ? top + (1 - (0 - r.lo) / (r.hi - r.lo)) * ROW_H : null,
        last: traces?.last ? path(traces.last, r.key, top, r.lo, r.hi) : "",
        best: traces?.best ? path(traces.best, r.key, top, r.lo, r.hi) : "",
      };
    }),
  );

  // Distance ticks in metres, read off the last lap (the axis the driver knows).
  const ticks = $derived.by(() => {
    const len = traces?.last?.length ?? 0;
    if (!len) return [];
    return [0, 0.25, 0.5, 0.75, 1].map((f) => ({
      x: f * W,
      label: `${Math.round((f * len) / 100) / 10}km`,
      // End ticks hug the edges instead of hanging half off the plot.
      shift: f === 0 ? "0" : f === 1 ? "-100%" : "-50%",
    }));
  });

  function lapTime(s: number): string {
    const m = Math.floor(s / 60);
    return `${m}:${(s - m * 60).toFixed(3).padStart(6, "0")}`;
  }
</script>

{#if !traces?.last}
  <p class="empty" data-testid="inputs-empty">
    Driver inputs appear here once you complete a lap.
  </p>
{:else}
  <div class="wrap">
    <ul class="legend">
      <li><span class="sw last"></span>Last <b data-testid="inputs-last-time">{lapTime(traces.last.lap_time)}</b></li>
      {#if traces.best}
        <li><span class="sw best"></span>Best <b data-testid="inputs-best-time">{lapTime(traces.best.lap_time)}</b></li>
      {/if}
    </ul>
    <div class="plot">
      <svg viewBox="0 0 {W} {H}" preserveAspectRatio="none" role="img"
           aria-label="Driver inputs against lap distance">
        {#each rows as r (r.key)}
          <rect x="0" y={r.top} width={W} height={ROW_H} class="bed" />
          {#if r.zero !== null}
            <line x1="0" y1={r.zero} x2={W} y2={r.zero} class="grid" />
          {/if}
          {#if r.best}
            <path d={r.best} class="line best" data-testid="trace-{r.key}-best" />
          {/if}
          <path d={r.last} class="line last" data-testid="trace-{r.key}-last" />
        {/each}
        {#each ticks as t}
          <line x1={t.x} y1="0" x2={t.x} y2={H - AXIS_H} class="grid xgrid" />
        {/each}
      </svg>
      <div class="labels" aria-hidden="true">
        {#each rows as r (r.key)}
          <span style="top:{(r.top / H) * 100}%">{r.label}</span>
        {/each}
      </div>
    </div>
    <ul class="xaxis" aria-hidden="true">
      {#each ticks as t}
        <li style="left:{(t.x / W) * 100}%; transform:translateX({t.shift})">{t.label}</li>
      {/each}
    </ul>
  </div>
{/if}

<style>
  .wrap {
    display: flex;
    flex-direction: column;
    gap: 4px;
    min-height: 0;
  }
  .empty {
    margin: 0;
    padding: 14px 0;
    text-align: center;
    color: var(--faint);
    font-size: 12px;
  }
  .plot {
    position: relative;
    flex: 1 1 auto;
    min-height: 0;
  }
  svg {
    display: block;
    width: 100%;
    height: 100%;
    min-height: 108px;
  }
  .bed {
    fill: var(--surface-2);
  }
  .grid {
    stroke: var(--line);
    vector-effect: non-scaling-stroke;
  }
  .xgrid {
    opacity: 0.6;
  }
  .line {
    fill: none;
    stroke-width: 1.5;
    stroke-linejoin: round;
    vector-effect: non-scaling-stroke;
  }
  .line.last {
    stroke: var(--accent);
  }
  .line.best {
    stroke: var(--good);
    opacity: 0.85;
  }
  .labels span {
    position: absolute;
    left: 6px;
    font-size: 9px;
    letter-spacing: 0.06em;
    text-transform: uppercase;
    color: var(--faint);
    font-family: var(--mono);
    pointer-events: none;
  }
  .legend {
    display: flex;
    gap: 14px;
    margin: 0;
    padding: 0;
    list-style: none;
    font-size: 11px;
    color: var(--muted);
  }
  .legend li {
    display: flex;
    align-items: center;
    gap: 5px;
  }
  .legend b {
    font-family: var(--mono);
    font-weight: 500;
    color: var(--text);
  }
  .sw {
    width: 10px;
    height: 2px;
    border-radius: 1px;
  }
  .sw.last {
    background: var(--accent);
  }
  .sw.best {
    background: var(--good);
  }
  .xaxis {
    position: relative;
    height: 12px;
    margin: 0;
    padding: 0;
    list-style: none;
  }
  .xaxis li {
    position: absolute;
    font-size: 9px;
    font-family: var(--mono);
    color: var(--faint);
  }
</style>
