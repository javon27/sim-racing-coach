<script lang="ts">
  import type { SessionState } from "./live";

  let { session }: { session: SessionState | null } = $props();

  // Returns [text, kind] where kind ∈ "good"|"bad"|"none".
  function delta(cur: number | null, best: number | null): [string, string] {
    if (cur == null || best == null) return ["—", "none"];
    const d = cur - best;
    const s = `${d >= 0 ? "+" : "-"}${Math.abs(d).toFixed(3)}`;
    return [s, d <= 0 ? "good" : "bad"];
  }

  const s1 = $derived(delta(session?.cur_s1 ?? null, session?.best_s1 ?? null));
  const s2 = $derived(
    delta(
      session?.cur_s2 != null && session?.cur_s1 != null ? session.cur_s2 - session.cur_s1 : null,
      session?.best_s2 != null && session?.best_s1 != null ? session.best_s2 - session.best_s1 : null,
    ),
  );
  // S3 live requires lap completion (the sector closes at the line) — deferred seam.
  const s3: [string, string] = ["—", "none"];
  const total = $derived(delta(session?.last_lap ?? null, session?.best_lap ?? null));
</script>

<div class="sectors">
  <div class="sector"><span class="s">S1</span><span class="d {s1[1]}" data-testid="sec-s1">{s1[0]}</span></div>
  <div class="sector"><span class="s">S2</span><span class="d {s2[1]}" data-testid="sec-s2">{s2[0]}</span></div>
  <div class="sector"><span class="s">S3</span><span class="d {s3[1]}" data-testid="sec-s3">{s3[0]}</span></div>
  <div class="sector total"><span class="s">Lap</span><span class="d {total[1]}" data-testid="sec-total">{total[0]}</span></div>
</div>

<style>
  .sectors { display: flex; gap: 1px; background: var(--line); border: 1px solid var(--line); border-radius: 8px; overflow: hidden; }
  .sector { flex: 1; background: var(--surface); padding: 8px 14px; display: flex; align-items: baseline; gap: 10px; }
  .sector.total { background: var(--surface-2, var(--surface)); }
  .s { font-size: 10px; text-transform: uppercase; letter-spacing: .12em; color: var(--faint); }
  .d { margin-left: auto; font-weight: 600; font-variant-numeric: tabular-nums; }
  .d.good { color: var(--good); }
  .d.bad { color: var(--bad); }
  .d.none { color: var(--faint); }
</style>
