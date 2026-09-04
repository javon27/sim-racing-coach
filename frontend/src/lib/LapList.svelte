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
  onDestroy(() => {
    if (pollTimer !== undefined) clearInterval(pollTimer);
  });

  function formatTime(seconds: number): string {
    const m = Math.floor(seconds / 60);
    const s = (seconds - m * 60).toFixed(3);
    return `${m}:${s.padStart(6, "0")}`;
  }

  function select(lapId: number) {
    onselect?.({ lap_id: lapId, session_id: currentSessionId ?? "" });
  }
</script>

<div class="rail-head">
  <h2>This session</h2>
  <span>{laps.length} laps</span>
</div>
<div class="laps">
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
    {#each laps as lap (lap.id)}
      <button type="button" class="lap" class:best={lap.id === bestId} onclick={() => select(lap.id)}>
        <span class="n">Lap {lap.id}</span>
        <span class="t">{formatTime(lap.lap_time)}</span>
        <span class="flag">{lap.valid ? "" : "✗ inv"}</span>
      </button>
    {/each}
  {/if}
</div>
