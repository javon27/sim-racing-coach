<script lang="ts">
  import { onDestroy } from "svelte";
  import { getStatus, getSessionLaps, withRetry, type LapRow } from "./api";

  let {
    onselect,
    retryTries = 8,
    retryDelayMs = 750,
    pollMs = 5000,
    sessionId = null,
    selectedLapId = null,
  }: {
    onselect?: (detail: { lap_id: number; session_id: string }) => void;
    retryTries?: number;
    retryDelayMs?: number;
    pollMs?: number;
    sessionId?: string | null;
    selectedLapId?: number | null;
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
    // Fast path: a clicked/deep-linked session — render its laps IMMEDIATELY via a
    // single indexed query, without gating on the (live-LMU, possibly slow) /status
    // call that fetchLatest does first. fetchLatest still runs after to set the live
    // baseline + start polling; it re-fetches the same rows (no flicker).
    if (reviewTarget !== null) {
      try {
        const l = await getSessionLaps(reviewTarget);
        laps = l;
        bestId = bestIdOf(l);
        currentSessionId = reviewTarget;
        phase = "ok";
      } catch {
        /* fall through to the retry path below */
      }
    }
    try {
      await withRetry(fetchLatest, retryTries, retryDelayMs);
      phase = "ok";
      startPolling();
    } catch {
      if (reviewTarget === null) {
        laps = [];
        phase = "unreachable";
      }
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

  // Reference for sector deltas = the FASTEST lap that has sector data. Comparing
  // each lap's sectors against that lap (not a per-sector min) is what makes a lap
  // able to be faster (down/green) or slower (up/red) sector by sector — matching
  // the prototype's delta view.
  const bests = $derived.by(() => {
    let ref: LapRow | null = null;
    for (const l of laps) {
      if (l.s1 == null && l.s2 == null && l.s3 == null) continue;
      if (ref === null || l.lap_time < ref.lap_time) ref = l;
    }
    return { s1: ref?.s1 ?? null, s2: ref?.s2 ?? null, s3: ref?.s3 ?? null };
  });

  // Per-sector delta vs the reference lap. cls drives color: pb (matches ref) /
  // up (slower, red) / down (faster than the reference lap, green).
  type SecInfo = { label: string; cls: string; delta: string };
  function sectorCell(label: string, v: number | null, b: number | null): SecInfo {
    if (v == null || b == null) return { label, cls: "", delta: "—" };
    if (v === b) return { label, cls: "pb", delta: "best" };
    const d = v - b;
    return { label, cls: d > 0 ? "up" : "down", delta: (d < 0 ? "−" : "+") + Math.abs(d).toFixed(2) };
  }
  function sectorInfo(lap: LapRow): SecInfo[] {
    return [
      sectorCell("S1", lap.s1, bests.s1),
      sectorCell("S2", lap.s2, bests.s2),
      sectorCell("S3", lap.s3, bests.s3),
    ];
  }

  // Per-lap expand state for the sector-detail panel (independent of selection).
  let expanded: Set<number> = $state(new Set());
  function toggleDetail(lapId: number) {
    const next = new Set(expanded);
    if (next.has(lapId)) next.delete(lapId);
    else next.add(lapId);
    expanded = next;
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
        {@const sectors = hasSectors(lap) ? sectorInfo(lap) : null}
        {@const open = expanded.has(lap.id)}
        <div class="lapwrap" class:open>
          <div
            class="lap"
            class:best={lap.id === bestId}
            class:selected={lap.id === selectedLapId}
          >
            {#if sectors}
              <!-- Left hit zone: select the lap. -->
              <button
                type="button"
                class="lap-select"
                aria-pressed={lap.id === selectedLapId}
                onclick={() => select(lap.id)}
              >
                <span class="n">
                  Lap {i + 1}
                  <span class="lapsec">
                    {#each sectors as s}
                      <i class={s.cls} title="{s.label} {s.delta}"></i>
                    {/each}
                  </span>
                </span>
              </button>
              <!-- Right hit zone (whole rest of the row): toggle sector detail. -->
              <button
                type="button"
                class="lap-expand"
                aria-label="Toggle sector detail"
                aria-expanded={open}
                onclick={() => toggleDetail(lap.id)}
              >
                <span class="t">{formatTime(lap.lap_time)}</span>
                <span class="flag">{lap.valid ? "" : "✗ inv"}</span>
                <span class="chev">
                  <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"><path d="M9 6l6 6-6 6" /></svg>
                </span>
              </button>
            {:else}
              <!-- No sectors: nothing to expand, so the whole row selects. -->
              <button
                type="button"
                class="lap-select full"
                aria-pressed={lap.id === selectedLapId}
                onclick={() => select(lap.id)}
              >
                <span class="n">Lap {i + 1}</span>
                <span class="t">{formatTime(lap.lap_time)}</span>
                <span class="flag">{lap.valid ? "" : "✗ inv"}</span>
              </button>
            {/if}
          </div>
          {#if sectors}
            <div class="lapdetail">
              <div class="secgrid">
                {#each sectors as s}
                  <div class="seccell">
                    <span class="sl">{s.label}</span>
                    <span class="sv {s.cls}">{s.delta}</span>
                  </div>
                {/each}
              </div>
            </div>
          {/if}
        </div>
      {/each}
    {/each}
  {/if}
</div>
