<!-- frontend/src/lib/InputTraces.svelte
     Driver-input graphs. Two modes:

     - "band" (default): the Pit Wall (#21) — last lap vs best, drawn as a
       filled min/max envelope per bin (`traces: InputTraces`, unchanged).
     - "line" (#95): the coaching screen's Inputs panel — one selected lap vs
       its track/car best, drawn as plain `mean` polylines plus a 4th speed
       row and corner apex markers (`traces: InputTrace`, `best: InputTrace`).

     DUMB BY DESIGN. The sidecar does the de-staircasing of the 5 Hz
     lap_distance channel and the reduction to bins; this file only maps
     numbers to an SVG path. No resampling, no smoothing, no analysis here —
     that belongs in the core.

     Band mode: each channel arrives as a per-bin min/max BAND, because one bin
     spans 180-400 ms — at or coarser than the game's 200 ms scoring tick — so
     a point sample would drop a stab or a lift depending on phase. Drawn as a
     filled polygon (hi edge out, lo edge back) that is also stroked, so a
     steady input, where the two edges coincide, still draws as a line.

     Line mode draws the bin `mean` as a plain stroked polyline instead — the
     driver wants to read a clean input trace, not the min/max ribbon.

     Laps are paired by NORMALIZED lap progress, the same convention ribbon.py
     uses: both laps start at the S/F line, so t = (d - d0) / (dN - d0) lines
     them up even when their measured lengths differ by a few metres. -->
<script lang="ts">
  import type { CornerMarker, InputBand, InputTrace, InputTraces as BandTraces } from "./api";

  let {
    traces = null,
    best = null,
    corners = [],
    mode = "band",
  }: {
    traces?: BandTraces | InputTrace | null;
    best?: InputTrace | null;
    corners?: CornerMarker[];
    mode?: "band" | "line";
  } = $props();

  const W = 1000;
  const ROW_H = 46;
  const GAP = 4;
  const AXIS_H = 16;
  // Reserve room above the rows for corner T-labels (line mode only).
  const MARKER_H = 12;

  type Row = { key: string; label: string; lo: number; hi: number };

  // [key, label, y-domain]. Steering is signed (full lock either way); the pedals
  // are 0..1, drawn on a fixed domain so a light lap doesn't rescale to look heavy.
  const BASE_ROWS: Row[] = [
    { key: "throttle", label: "Throttle", lo: 0, hi: 1 },
    { key: "brake", label: "Brake", lo: 0, hi: 1 },
    { key: "steering", label: "Steer", lo: -1, hi: 1 },
  ];

  // In band mode `traces` is the {last, best} wrapper; in line mode it IS the
  // single lap trace and `best` is a separate prop. Branch on `mode`, the way
  // every caller already picks a shape for it.
  const primary = $derived.by((): InputTrace | null => {
    if (mode === "line") {
      const t = traces as InputTrace | null;
      return t && Array.isArray(t.dist) && t.dist.length ? t : null;
    }
    return (traces as BandTraces | null)?.last ?? null;
  });
  const reference = $derived.by((): InputTrace | null => {
    if (mode === "line") return best;
    return (traces as BandTraces | null)?.best ?? null;
  });

  const hasMarkers = $derived(mode === "line" && corners.length > 0);
  const topPad = $derived(hasMarkers ? MARKER_H : 0);

  const activeRows = $derived.by((): Row[] => {
    if (mode !== "line") return BASE_ROWS;
    const max = (b: InputBand | undefined) => (b?.mean.length ? Math.max(...b.mean) : 0);
    const speedHi = Math.max(1, max(primary?.speed), max(reference?.speed));
    return [...BASE_ROWS, { key: "speed", label: "Speed", lo: 0, hi: speedHi }];
  });

  const H = $derived(
    topPad + activeRows.length * ROW_H + (activeRows.length - 1) * GAP + AXIS_H,
  );

  function band(tr: InputTrace | null, key: string, top: number, lo: number, hi: number): string {
    if (!tr) return "";
    const b = (tr as unknown as Record<string, InputBand | undefined>)[key];
    const d = tr.dist;
    if (!b || !d) return "";
    const n = Math.min(d.length, b.lo.length, b.hi.length);
    if (n < 2) return "";
    const d0 = d[0];
    const span = d[n - 1] - d0 || 1;
    const rng = hi - lo || 1;
    const px = (i: number) => (((d[i] - d0) / span) * W).toFixed(1);
    const py = (v: number) => (top + (1 - (v - lo) / rng) * ROW_H).toFixed(1);
    const out: string[] = [];
    for (let i = 0; i < n; i++) out.push(`${i ? "L" : "M"}${px(i)} ${py(b.hi[i])}`);
    for (let i = n - 1; i >= 0; i--) out.push(`L${px(i)} ${py(b.lo[i])}`);
    return `${out.join(" ")} Z`;
  }

  // Same x/y mapping as band(), but a plain polyline through the bin MEAN — no
  // envelope, no closing edge.
  function line(tr: InputTrace | null, key: string, top: number, lo: number, hi: number): string {
    if (!tr) return "";
    const b = (tr as unknown as Record<string, InputBand | undefined>)[key];
    const d = tr.dist;
    if (!b?.mean || !d) return "";
    const n = Math.min(d.length, b.mean.length);
    if (n < 2) return "";
    const d0 = d[0];
    const span = d[n - 1] - d0 || 1;
    const rng = hi - lo || 1;
    const px = (i: number) => (((d[i] - d0) / span) * W).toFixed(1);
    const py = (v: number) => (top + (1 - (v - lo) / rng) * ROW_H).toFixed(1);
    const out: string[] = [];
    for (let i = 0; i < n; i++) out.push(`${i ? "L" : "M"}${px(i)} ${py(b.mean[i])}`);
    return out.join(" ");
  }

  const rows = $derived(
    activeRows.map((r, i) => {
      const top = topPad + i * (ROW_H + GAP);
      const draw = mode === "line" ? line : band;
      return {
        ...r,
        top,
        // Zero line only where it sits inside the plot (steering); pedals rest on the floor.
        zero: r.lo < 0 ? top + (1 - (0 - r.lo) / (r.hi - r.lo)) * ROW_H : null,
        last: draw(primary, r.key, top, r.lo, r.hi),
        best: draw(reference, r.key, top, r.lo, r.hi),
      };
    }),
  );

  // Distance ticks in metres, read off the primary trace (the axis the driver knows).
  // Band mode only — line mode's InputsPanel header carries the context instead.
  const ticks = $derived.by(() => {
    if (mode === "line") return [];
    const len = primary?.length ?? 0;
    if (!len) return [];
    return [0, 0.25, 0.5, 0.75, 1].map((f) => ({
      x: f * W,
      label: `${Math.round((f * len) / 100) / 10}km`,
      // End ticks hug the edges instead of hanging half off the plot.
      shift: f === 0 ? "0" : f === 1 ? "-100%" : "-50%",
    }));
  });

  // Corner apex ticks + window shading, positioned on the same normalized x
  // axis as the traces (both start_dist/apex_dist/end_dist and `dist` are
  // absolute lap-distance metres from the primary trace's own lap).
  const markers = $derived.by(() => {
    if (!hasMarkers) return [];
    const d = primary?.dist ?? [];
    if (d.length < 2) return [];
    const d0 = d[0];
    const span = d[d.length - 1] - d0 || 1;
    const x = (v: number) => ((v - d0) / span) * W;
    // "4" reads as T4 on the plot; a name like "Eau Rouge" is left alone.
    const raw = corners
      .map((c) => ({
        label: c.label,
        displayLabel: /^\d+[A-Za-z]?$/.test(c.label) ? `T${c.label}` : c.label,
        x: x(c.apex_dist),
        xStart: x(c.start_dist),
        xEnd: x(c.end_dist),
      }))
      .sort((a, b) => a.x - b.x);
    // Narrow panels crowd adjacent T-labels; skip every other one where two
    // consecutive apexes sit under ~24 px apart (measured in viewBox units).
    return raw.map((m, i) => {
      const prev = raw[i - 1];
      const tight = i > 0 && m.x - prev.x < 24;
      return { ...m, showLabel: !(tight && i % 2 === 1) };
    });
  });

  function lapTime(s: number): string {
    const m = Math.floor(s / 60);
    return `${m}:${(s - m * 60).toFixed(3).padStart(6, "0")}`;
  }
</script>

{#if !primary}
  <p class="empty" data-testid="inputs-empty">
    Driver inputs appear here once you complete a lap.
  </p>
{:else}
  <div class="wrap">
    {#if mode !== "line"}
      <ul class="legend">
        <li><span class="sw last"></span>Last <b data-testid="inputs-last-time">{lapTime(primary.lap_time)}</b></li>
        {#if reference}
          <li><span class="sw best"></span>Best <b data-testid="inputs-best-time">{lapTime(reference.lap_time)}</b></li>
        {/if}
      </ul>
    {/if}
    <div class="plot">
      <svg viewBox="0 0 {W} {H}" preserveAspectRatio="none" role="img"
           aria-label="Driver inputs against lap distance">
        {#if hasMarkers}
          {#each markers as m (m.label)}
            <rect x={Math.min(m.xStart, m.xEnd)} y={topPad} width={Math.abs(m.xEnd - m.xStart)}
                  height={H - AXIS_H - topPad} class="corner-band" />
          {/each}
        {/if}
        {#each rows as r (r.key)}
          <rect x="0" y={r.top} width={W} height={ROW_H} class="bed" />
          {#if r.zero !== null}
            <line x1="0" y1={r.zero} x2={W} y2={r.zero} class="grid" />
          {/if}
          {#if r.best}
            <path d={r.best} class="{mode === 'line' ? 'mline dashed' : 'line'} best" data-testid="trace-{r.key}-best" />
          {/if}
          <path d={r.last} class="{mode === 'line' ? 'mline' : 'line'} last" data-testid="trace-{r.key}-last" />
        {/each}
        {#if hasMarkers}
          {#each markers as m (m.label)}
            <line x1={m.x} y1={topPad} x2={m.x} y2={H - AXIS_H} class="corner-tick" data-testid="corner-tick-{m.label}" />
            {#if m.showLabel}
              <text x={m.x} y={topPad - 3} text-anchor="middle" class="corner-label" data-testid="corner-label-{m.label}">{m.displayLabel}</text>
            {/if}
          {/each}
        {/if}
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
    {#if mode !== "line"}
      <ul class="xaxis" aria-hidden="true">
        {#each ticks as t}
          <li style="left:{(t.x / W) * 100}%; transform:translateX({t.shift})">{t.label}</li>
        {/each}
      </ul>
    {/if}
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
    /* #222: height otherwise follows the viewBox ratio and grows with width
       (~670px on a 3440 ultrawide); 20vh = 288px at 1440p. */
    max-height: 20vh;
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
  /* Filled band + stroked outline: a steady input collapses the band to zero
     height, and the stroke keeps it visible as a line. */
  .line {
    stroke-width: 1.2;
    stroke-linejoin: round;
    vector-effect: non-scaling-stroke;
  }
  .line.last {
    stroke: var(--accent);
    fill: var(--accent);
    fill-opacity: 0.35;
  }
  .line.best {
    stroke: var(--good);
    fill: var(--good);
    fill-opacity: 0.22;
    opacity: 0.85;
  }
  /* Plain mean-value polyline (line mode) — no fill, the driver reads a clean
     trace instead of a min/max ribbon. */
  .mline {
    fill: none;
    stroke-linejoin: round;
    vector-effect: non-scaling-stroke;
  }
  .mline.last {
    stroke: var(--accent);
    stroke-width: 1.6;
  }
  .mline.best {
    stroke: var(--trace-best);
    stroke-width: 1.2;
    opacity: 1;
  }
  .mline.dashed {
    stroke-dasharray: 4 3;
  }
  .corner-band {
    fill: var(--muted);
    fill-opacity: 0.1;
  }
  .corner-tick {
    stroke: var(--faint);
    stroke-dasharray: 2 2;
    vector-effect: non-scaling-stroke;
  }
  .corner-label {
    font-size: 9px;
    font-family: var(--mono);
    fill: var(--faint);
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
