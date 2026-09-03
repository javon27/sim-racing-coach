<script lang="ts">
  import { onDestroy } from "svelte";
  import { getSessions, getSessionLaps, withRetry, type LapRow } from "./api";

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

  function bestIdOf(l: LapRow[]): number | null {
    return (
      l.reduce<LapRow | null>(
        (best, lap) => (best === null || lap.lap_time < best.lap_time ? lap : best),
        null,
      )?.id ?? null
    );
  }

  async function fetchLatest() {
    if (sessionId) {
      currentSessionId = sessionId;
      const l = await getSessionLaps(sessionId);
      laps = l;
      bestId = bestIdOf(l);
      return;
    }
    const sessions = await getSessions();
    if (sessions.length === 0) {
      laps = [];
      bestId = null;
      currentSessionId = null;
      return;
    }
    const latest = [...sessions].sort((a, b) => b.last - a.last)[0];
    currentSessionId = latest.session_id;
    const l = await getSessionLaps(latest.session_id);
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

  $effect(() => {
    sessionId;
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
