<script lang="ts">
  // Ported from docs/design/v1-ui-prototype.html svgChart() — best-lap-by-session
  // trend, one line per car. Pure presentation: caller filters/shapes the data.

  type Point = { date: string; value: number };
  type Series = { name: string; color?: string; points: Point[] };

  let {
    series = [],
    placeholder = "No laps recorded here yet",
  }: {
    series?: Series[];
    placeholder?: string;
  } = $props();

  const PALETTE = ["#6d4aff", "#0e9f6e", "#da2e2e", "#b45c09", "#0aa1c5"];

  function formatLap(seconds: number): string {
    const m = Math.floor(seconds / 60);
    const s = (seconds - m * 60).toFixed(3);
    return `${m}:${s.padStart(6, "0")}`;
  }

  const totalPoints = $derived(series.reduce((n, s) => n + s.points.length, 0));

  const chart = $derived.by(() => {
    const W = 720, H = 250, L = 64, R = 18, T = 16, B = 34;
    const plotW = W - L - R, plotH = H - T - B;
    const allY = series.flatMap((s) => s.points.map((p) => p.value));
    const mn = Math.min(...allY);
    const mx = Math.max(...allY);
    const rng = mx - mn || 1;
    const days = [...new Set(series.flatMap((s) => s.points.map((p) => p.date)))];
    const x = (i: number) => L + (days.length < 2 ? plotW / 2 : (i / (days.length - 1)) * plotW);
    const y = (v: number) => T + (1 - (v - mn) / rng) * plotH;

    const gridLines = Array.from({ length: 4 }, (_, g) => {
      const yy = T + (g / 3) * plotH;
      const val = mx - (g / 3) * rng;
      return { y: yy, label: formatLap(val) };
    });

    const lines = series.map((s, i) => {
      const color = s.color ?? PALETTE[i % PALETTE.length];
      const pts = s.points.map((p) => [x(days.indexOf(p.date)), y(p.value)] as const);
      const path = pts.map((p, i2) => `${i2 ? "L" : "M"}${p[0].toFixed(1)} ${p[1].toFixed(1)}`).join(" ");
      return { name: s.name, color, path, dots: pts };
    });

    const xLabels = days.map((d, i) => ({ x: x(i), label: d }));

    return { W, H, L, R, gridLines, lines, xLabels };
  });
</script>

{#if totalPoints < 1}
  <div class="chart-empty">{placeholder}</div>
{:else}
  <svg viewBox="0 0 {chart.W} {chart.H}" width="100%" role="img" aria-label="Best lap trend">
    {#each chart.gridLines as g}
      <line x1={chart.L} y1={g.y} x2={chart.W - chart.R} y2={g.y} class="grid" />
      <text x={chart.L - 8} y={g.y + 3} text-anchor="end" class="ylabel">{g.label}</text>
    {/each}
    {#each chart.lines as line (line.name)}
      <path d={line.path} fill="none" stroke={line.color} stroke-width="2.5" stroke-linejoin="round" stroke-linecap="round" />
      {#each line.dots as d}
        <circle cx={d[0].toFixed(1)} cy={d[1].toFixed(1)} r="3.5" fill={line.color} class="dot" />
      {/each}
    {/each}
    {#each chart.xLabels as xl}
      <text x={xl.x} y={chart.H - 12} text-anchor="middle" class="xlabel">{xl.label}</text>
    {/each}
  </svg>
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
