<script lang="ts">
  import { onDestroy } from "svelte";
  import { getStatus, getSessionLaps, withRetry, type LapRow } from "./api";

  let {
    onselect,
    retryTries = 8,
    retryDelayMs = 750,
    pollMs = 5000,
    sessionId = null,
  }: {
    onselect?: (detail: { lap_id: number; session_id: string }) => void;
    retryTries?: number;
    retryDelayMs?: number;
    pollMs?: number;
    sessionId?: string | null;
  } = $props();

  let laps: LapRow[] = $state([]);
  let bestId: number | null = $state(null);
  let currentSessionId: string | null = $state(null);
  let phase: "connecting" | "ok" | "unreachable" = $state("connecting");
  let pollTimer: ReturnType<typeof setInterval> | undefined;

  // Auto-scroll: keep the newest lap in view as laps are appended (session_laps
  // is ordered by ts, newest last).
  let lapsEl: HTMLDivElement | undefined = $state();
  let prevCount = 0;

  const bestTime = $derived(laps.find((l) => l.id === bestId)?.lap_time ?? null);

  // Group consecutive laps by phase (a session spans practice/qualy/race in
  // chronological blocks) and number laps 1..N within each phase.
  type LapGroup = { phase: string | null; laps: LapRow[] };
  const groups = $derived.by<LapGroup[]>(() => {
    const out: LapGroup[] = [];
    let cur: LapGroup | null = null;
    for (const lap of laps) {
      const p = lap.phase ?? null;
      if (!cur || cur.phase !== p) {
        cur = { phase: p, laps: [] };
        out.push(cur);
      }
      cur.laps.push(lap);
    }
    return out;
  });
  function phaseLabel(p: string | null): string {
    return (p ?? "laps").toUpperCase();
  }

  // Review state: `sessionId` prop is the review target (a past session deep-linked
  // from Sessions). We hold on it until a NEW live lap arrives past the baseline
  // (the live lap_id captured on the first poll after entering review).
  let reviewTarget: string | null = null;
  let reviewBaseline: number | null = null;
  let reviewBaselineSet = false;

  function bestIdOf(l: LapRow[]): number | null {
    return (
      l.reduce<LapRow | null>(
        (best, lap) => (best === null || lap.lap_time < best.lap_time ? lap : best),
        null,
      )?.id ?? null
    );
  }

  async function fetchLatest() {
    const st = await getStatus();
    const liveSessionId = st.session_id;
    const liveLapId = st.lap_id;

    if (reviewTarget !== null && !reviewBaselineSet) {
      reviewBaseline = liveLapId;
      reviewBaselineSet = true;
    }

    // Hold on the review target until a live lap advances past the baseline.
    const reviewing =
      reviewTarget !== null &&
      !(liveLapId !== null && (reviewBaseline === null || liveLapId > reviewBaseline));
    const target = reviewing ? reviewTarget : liveSessionId;

    if (target === null) {
      laps = [];
      bestId = null;
      currentSessionId = null;
      return;
    }
    currentSessionId = target;
    const l = await getSessionLaps(target);
    laps = l;
    bestId = bestIdOf(l);
  }

  async function poll() {
    try {
      await fetchLatest();
    } catch {
      // transient poll failure - keep last good data, don't touch phase
    }
  }

  function startPolling() {
    if (pollTimer !== undefined) return;
    pollTimer = setInterval(poll, pollMs);
  }

  async function load() {
    phase = "connecting";
    try {
      await withRetry(fetchLatest, retryTries, retryDelayMs);
      phase = "ok";
      startPolling();
    } catch {
      laps = [];
      phase = "unreachable";
    }
  }

  // Keyed on the prop: entering (or re-entering) review resets the baseline so
  // the next poll re-snapshots the live lap_id.
  $effect(() => {
    reviewTarget = sessionId ?? null;
    reviewBaseline = null;
    reviewBaselineSet = false;
    load();
  });
  // Scroll to the newest lap when the count grows (new lap captured, or first load).
  $effect(() => {
    if (laps.length > prevCount && lapsEl) {
      lapsEl.scrollTop = lapsEl.scrollHeight;
    }
    prevCount = laps.length;
  });

  onDestroy(() => {
    if (pollTimer !== undefined) clearInterval(pollTimer);
  });

  function formatTime(seconds: number): string {
    // Round to ms before splitting so e.g. 119.9996 -> "2:00.000", not "1:60.000".
    const totalMs = Math.round(seconds * 1000);
    const m = Math.floor(totalMs / 60000);
    const s = ((totalMs - m * 60000) / 1000).toFixed(3);
    return `${m}:${s.padStart(6, "0")}`;
  }

  // `== null` catches both null (API: no sectors) and undefined (defensive).
  function hasSectors(lap: LapRow): boolean {
    return lap.s1 != null || lap.s2 != null || lap.s3 != null;
  }
  function fmtSector(s: number | null): string {
    return s == null ? "—" : s.toFixed(1);
  }

  function select(lapId: number) {
    onselect?.({ lap_id: lapId, session_id: currentSessionId ?? "" });
  }
</script>

<div class="rail-head">
  <h2>This session</h2>
  <span>{laps.length} laps</span>
</div>
{#if bestTime !== null}
  <div class="best-head">
    <span class="lbl">Best</span>
    <span class="t">{formatTime(bestTime)}</span>
  </div>
{/if}
<div class="laps" bind:this={lapsEl}>
  {#if phase === "connecting"}
    <p class="empty">Connecting to the coach server…</p>
  {:else if phase === "unreachable"}
    <p class="empty">
      Can't reach the coach server — is it running?
      <button type="button" onclick={load}>Retry</button>
    </p>
  {:else if laps.length === 0}
    <p class="empty">No laps yet</p>
  {:else}
    {#each groups as group, gi (gi)}
      {#if group.phase}
        <div class="phase-head">{phaseLabel(group.phase)}</div>
      {/if}
      {#each group.laps as lap, i (lap.id)}
        <button type="button" class="lap" class:best={lap.id === bestId} onclick={() => select(lap.id)}>
          <span class="n">Lap {i + 1}</span>
          <span class="t">{formatTime(lap.lap_time)}</span>
          <span class="flag">{lap.valid ? "" : "✗ inv"}</span>
          {#if hasSectors(lap)}
            <span class="sectors">{fmtSector(lap.s1)} · {fmtSector(lap.s2)} · {fmtSector(lap.s3)}</span>
          {/if}
        </button>
      {/each}
    {/each}
  {/if}
</div>
