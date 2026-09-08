<script lang="ts">
  import type { SessionState } from "./live";

  // classBest: session-class best per-sector SPLITS + lap (fastest by anyone in the
  // player's class), the purple reference. null when there's no field/player.
  let { session, classBest = null }: {
    session: SessionState | null;
    classBest?: { s1: number | null; s2: number | null; s3: number | null; lap: number | null } | null;
  } = $props();

  function fmt(s: number | null | undefined): string {
    if (s == null || s <= 0) return "—";
    const ms = Math.round(s * 1000);
    const m = Math.floor(ms / 60000);
    return `${m}:${((ms - m * 60000) / 1000).toFixed(3).padStart(6, "0")}`;
  }
  function cmp(cur: number | null, best: number | null): string {
    if (cur == null || cur <= 0 || best == null || best <= 0) return "";
    return cur <= best ? "good" : "bad";
  }
  // The player's own best split: purple only if it ties the session-class best,
  // else green (a personal best that isn't the session's fastest).
  function bestKind(v: number | null, classSplit: number | null | undefined): string {
    if (v == null || v <= 0) return "";
    if (classSplit != null && classSplit > 0 && v <= classSplit + 0.001) return "best";
    return "good";
  }
  // Per-sector split from cumulative fields: LMU reports cur/best S2 as (S1+S2).
  const sub = (a: number | null | undefined, b: number | null | undefined): number | null =>
    a != null && a > 0 && b != null && b > 0 ? a - b : null;

  const s = $derived(session);
  // Current lap (in progress): S1, then S2 split; S3 + total only exist once done.
  const curS1 = $derived(s?.cur_s1 ?? null);
  const curS2 = $derived(sub(s?.cur_s2, s?.cur_s1));
  // Last completed lap, broken into per-sector splits.
  const lastS1 = $derived(s?.last_s1 ?? null);
  const lastS2 = $derived(sub(s?.last_s2, s?.last_s1));
  const lastS3 = $derived(sub(s?.last_lap, s?.last_s2));
  // Best lap, broken into per-sector splits.
  const bestS1 = $derived(s?.best_s1 ?? null);
  const bestS2 = $derived(sub(s?.best_s2, s?.best_s1));
  const bestS3 = $derived(sub(s?.best_lap, s?.best_s2));
</script>

<div class="timing">
  <div class="row head">
    <span class="lbl">Sector</span><span class="t">S1</span><span class="t">S2</span><span class="t">S3</span><span class="t">Lap</span>
  </div>
  <div class="row">
    <span class="lbl">Current</span>
    <span class="t {cmp(curS1, bestS1)}" data-testid="cur-s1">{fmt(curS1)}</span>
    <span class="t {cmp(curS2, bestS2)}" data-testid="cur-s2">{fmt(curS2)}</span>
    <span class="t" data-testid="cur-s3">—</span>
    <span class="t" data-testid="cur-total">—</span>
  </div>
  <div class="row">
    <span class="lbl">Last</span>
    <span class="t {cmp(lastS1, bestS1)}" data-testid="last-s1">{fmt(lastS1)}</span>
    <span class="t {cmp(lastS2, bestS2)}" data-testid="last-s2">{fmt(lastS2)}</span>
    <span class="t {cmp(lastS3, bestS3)}" data-testid="last-s3">{fmt(lastS3)}</span>
    <span class="t {cmp(s?.last_lap ?? null, s?.best_lap ?? null)}" data-testid="last-total">{fmt(s?.last_lap)}</span>
  </div>
  <div class="row">
    <span class="lbl">Best</span>
    <span class="t {bestKind(bestS1, classBest?.s1)}" data-testid="best-s1">{fmt(bestS1)}</span>
    <span class="t {bestKind(bestS2, classBest?.s2)}" data-testid="best-s2">{fmt(bestS2)}</span>
    <span class="t {bestKind(bestS3, classBest?.s3)}" data-testid="best-s3">{fmt(bestS3)}</span>
    <span class="t {bestKind(s?.best_lap ?? null, classBest?.lap)}" data-testid="best-total">{fmt(s?.best_lap)}</span>
  </div>
  {#if classBest}
    <div class="row class-row">
      <span class="lbl">Class best</span>
      <span class="t best" data-testid="class-s1">{fmt(classBest.s1)}</span>
      <span class="t best" data-testid="class-s2">{fmt(classBest.s2)}</span>
      <span class="t best" data-testid="class-s3">{fmt(classBest.s3)}</span>
      <span class="t best" data-testid="class-total">{fmt(classBest.lap)}</span>
    </div>
  {/if}
</div>

<style>
  .timing {
    border: 1px solid var(--line);
    border-radius: 8px;
    overflow: hidden;
    background: var(--surface);
  }
  .row {
    display: grid;
    grid-template-columns: 1fr repeat(4, minmax(64px, auto));
    align-items: baseline;
    gap: 10px;
    padding: 6px 14px;
  }
  .row + .row { border-top: 1px solid var(--line); }
  /* Separate the field reference (Class best) from the player's own three rows. */
  .class-row { border-top: 2px solid var(--line); }
  .class-row .lbl { color: var(--purple, #a855f7); }
  .row.head { color: var(--faint); font-size: 10px; text-transform: uppercase; letter-spacing: .12em; }
  .lbl { font-weight: 700; font-size: 12px; }
  .t {
    text-align: right;
    font-variant-numeric: tabular-nums;
    font-size: 13px;
    font-weight: 600;
    color: var(--text);
  }
  .row.head .t { font-weight: 700; }
  .t.good { color: var(--good); }
  .t.bad { color: var(--bad); }
  .t.best { color: var(--purple, #a855f7); } /* personal best splits */
</style>
