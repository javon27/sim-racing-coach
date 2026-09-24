<script lang="ts">
  import { onDestroy } from "svelte";
  import { getSessionLaps, withRetry, type LapRow, type PartialKind, type StatusResponse } from "./api";
  import { eventFeed } from "./events";

  let {
    onselect,
    onlive,
    retryTries = 8,
    retryDelayMs = 750,
    sessionId = null,
    selectedLapId = null,
  }: {
    onselect?: (detail: { lap_id: number; session_id: string }) => void;
    onlive?: (detail: { session_id: string }) => void;
    retryTries?: number;
    retryDelayMs?: number;
    sessionId?: string | null;
    selectedLapId?: number | null;
  } = $props();

  let laps: LapRow[] = $state([]);
  let bestId: number | null = $state(null);
  // Plain `let`, deliberately NOT $state: nothing renders it, and fetchLatest both
  // reads and writes it from inside an $effect's synchronous prefix — as reactive
  // state that self-dependency would re-run the sessionId effect and re-arm the
  // review hold the moment the list snapped to a new live session.
  let currentSessionId: string | null = null;
  let pinnedLoaded: string | null = null; // the pinned session whose rows are already shown
  let phase: "connecting" | "ok" | "unreachable" = $state("connecting");

  // Auto-scroll: keep the newest lap in view as laps are appended (session_laps
  // is ordered by ts, newest last).
  let lapsEl: HTMLDivElement | undefined = $state();
  let prevCount = 0;

  const bestTime = $derived(laps.find((l) => l.id === bestId)?.lap_time ?? null);

  // Track/car for the shown session (session_id encodes one combo, so any lap's
  // pair labels the whole list — tells the driver which combo "This session" is).
  const combo = $derived.by(() => {
    const l = laps.find((x) => x.track || x.car);
    return l ? { track: l.track, car: l.car_model ?? l.car } : null;
  });

  // Group consecutive laps by (phase, race_index) -- a session spans practice/
  // qualy/race in chronological blocks, and #142: when it holds more than one
  // RACE (race_index from the server, never re-derived here -- see store.py
  // _race_windows_from_laps), a race-phase run also splits at a race boundary,
  // even one with no non-race lap between the two races (a pause-only gap).
  // Numbering restarts each group: prefers the game's own lap number (lap_no --
  // scoring mTotalLaps at the wrap, issue #77) when EVERY lap in the group has
  // one, so a garage return or an out-lap in between shows the game's real gaps
  // (e.g. 2,3,4,8,9); falls back to counting 1..N otherwise (a legacy session,
  // or any lap in the group still missing it) -- must agree with the backend's
  // CoachContext._lap_numbering.
  type NumberedLap = LapRow & { num: number };
  type LapGroup = { phase: string | null; raceIndex: number | null; laps: NumberedLap[] };
  // More than one race this session -> each race gets its own divider instead of
  // one shared "RACE" header (#142). A single race renders unchanged.
  const multiRace = $derived.by(
    () => new Set(laps.filter((l) => l.race_index != null).map((l) => l.race_index)).size > 1,
  );
  const groups = $derived.by<LapGroup[]>(() => {
    const out: { phase: string | null; raceIndex: number | null; laps: LapRow[] }[] = [];
    let cur: (typeof out)[number] | null = null;
    for (const lap of laps) {
      const p = lap.phase ?? null;
      const ri = p === "race" ? (lap.race_index ?? null) : null;
      if (!cur || cur.phase !== p || cur.raceIndex !== ri) {
        cur = { phase: p, raceIndex: ri, laps: [] };
        out.push(cur);
      }
      cur.laps.push(lap);
    }
    return out.map((g) => {
      const useLapNo = g.laps.every((l) => l.lap_no != null);
      return {
        phase: g.phase,
        raceIndex: g.raceIndex,
        laps: g.laps.map((l, i) => ({ ...l, num: useLapNo ? l.lap_no! : i + 1 })),
      };
    });
  });
  function phaseLabel(p: string | null): string {
    return (p ?? "laps").toUpperCase();
  }
  // "Race 2 — started 00:26" -- wording matches the coach's _lap_numbering label
  // ("race 2 lap 1"). ts is epoch seconds, same as the backend's time.localtime.
  function raceHeader(ts: number, raceIndex: number): string {
    const d = new Date(ts * 1000);
    const hh = String(d.getHours()).padStart(2, "0");
    const mm = String(d.getMinutes()).padStart(2, "0");
    return `Race ${raceIndex} — started ${hh}:${mm}`;
  }

  // Review state: `sessionId` prop is the review target (a past session deep-linked
  // from Sessions). We hold on it until a NEW live lap arrives past the baseline
  // (the live lap_id captured on the first poll after entering review).
  let reviewTarget: string | null = null;
  let reviewBaseline: number | null = null;
  let reviewBaselineSet = false;
  // The live session id observed when the baseline was captured -- a DIFFERENT
  // live session id showing up later ends the hold immediately, even before a
  // new lap arrives (the review target itself is a past session and will never
  // equal the live session id, so this can't be compared against reviewTarget).
  let reviewLiveSessionId: string | null = null;

  // A partial lap (#77) has lap_time null, and in JS `null < 92` is TRUE — an
  // unguarded comparison would crown every out-lap the session best. Skip them
  // outright: a lap with no time can never be the fastest one.
  function bestIdOf(l: LapRow[]): number | null {
    return (
      l.reduce<LapRow | null>(
        (best, lap) =>
          // A mapping lap (#154) is a boundary weave, never the best.
          lap.lap_time == null || lap.mapping
            ? best
            : best === null || lap.lap_time < best.lap_time! ? lap : best,
        null,
      )?.id ?? null
    );
  }

  async function fetchLatest(st: StatusResponse) {
    const liveSessionId = st.session_id;
    const liveLapId = st.lap_id;

    if (reviewTarget !== null && !reviewBaselineSet) {
      reviewBaseline = liveLapId;
      reviewBaselineSet = true;
      reviewLiveSessionId = liveSessionId;
    }

    // Hold on the review target until a live lap advances past the baseline, OR
    // until the live session id itself changes out from under it (a new live
    // session ends the hold immediately, even before its first lap arrives).
    const reviewing =
      reviewTarget !== null &&
      (liveSessionId === null || liveSessionId === reviewLiveSessionId) &&
      !(liveLapId !== null && (reviewBaseline === null || liveLapId > reviewBaseline));
    const target = reviewing ? reviewTarget : liveSessionId;

    if (target === null) {
      laps = [];
      bestId = null;
      currentSessionId = null;
      return;
    }
    const prev = currentSessionId;
    currentSessionId = target;
    if (!reviewing && prev !== null && target !== prev) onlive?.({ session_id: target });
    // The live session's rows ride along on the feed; only a session the feed
    // isn't streaming (a pinned past session) still needs the indexed query.
    const frame = eventFeed.laps.value;
    let l: LapRow[];
    if (frame && frame.session_id === target) {
      l = frame.laps;
    } else if (!(reviewing && pinnedLoaded === target)) {
      // A pinned past session's rows never change: query them once per target,
      // not once per 1 Hz status frame. A followed live session keeps refetching
      // until its laps frame arrives. (`pinnedLoaded` is a plain let on purpose,
      // and `laps` is never READ here: a $state read in this synchronous prefix
      // would make the status effect depend on it.)
      l = await getSessionLaps(target);
      pinnedLoaded = reviewing ? target : null;
    } else {
      return;
    }
    laps = l;
    bestId = bestIdOf(l);
  }

  // #106: this was a 5 s poll of getStatus + getSessionLaps. The status frame is
  // now the trigger, and the same fetchLatest logic runs on it unchanged, so the
  // review-hold / baseline / onlive semantics (#109) are untouched.
  let lastHandled: StatusResponse | null = null;
  async function handle(st: StatusResponse, force = false) {
    if (!force && st === lastHandled) return; // load() already ran this frame
    lastHandled = st;
    clearDead();
    try {
      await fetchLatest(st);
      phase = "ok";
    } catch {
      // transient failure - keep last good data, don't touch phase; the next
      // frame (≈1 s away) retries on its own
    }
  }

  // "Unreachable" is a feed story now: nothing has arrived within the grace the
  // old retry loop had (tries x delay). A pinned session never shows it — it has
  // its own rows and doesn't depend on live state.
  let deadTimer: ReturnType<typeof setTimeout> | undefined;
  function clearDead() {
    if (deadTimer !== undefined) clearTimeout(deadTimer);
    deadTimer = undefined;
  }
  function armDead() {
    clearDead();
    deadTimer = setTimeout(() => {
      deadTimer = undefined;
      if (phase !== "ok" && reviewTarget === null) {
        laps = [];
        phase = "unreachable";
      }
    }, retryTries * retryDelayMs);
  }

  function load() {
    phase = "connecting";
    const st = eventFeed.status.value;
    if (st) {
      void handle(st, true);
      return;
    }
    // No live state yet. A clicked/deep-linked session still renders its laps
    // IMMEDIATELY via a single indexed query rather than waiting on the feed.
    armDead();
    if (reviewTarget !== null) void loadPinned(reviewTarget);
  }

  async function loadPinned(target: string) {
    try {
      const l = await withRetry(() => getSessionLaps(target), retryTries, retryDelayMs);
      if (target !== reviewTarget) return; // the prop moved on while we fetched
      laps = l;
      bestId = bestIdOf(l);
      currentSessionId = target;
      phase = "ok";
    } catch {
      /* the next status frame picks it up */
    }
  }

  // Keyed on the prop: entering (or re-entering) review resets the baseline so
  // the next frame re-snapshots the live lap_id.
  $effect(() => {
    reviewTarget = sessionId ?? null;
    reviewBaseline = null;
    reviewBaselineSet = false;
    reviewLiveSessionId = null;
    pinnedLoaded = null;
    load();
  });

  $effect(() =>
    eventFeed.status.subscribe((st) => {
      if (st !== null) void handle(st);
    }),
  );
  // A laps frame for the session we're following updates the list on its own —
  // no status frame, no fetch.
  $effect(() =>
    eventFeed.laps.subscribe((f) => {
      if (!f || f.session_id === null || f.session_id !== currentSessionId) return;
      laps = f.laps;
      bestId = bestIdOf(f.laps);
    }),
  );
  $effect(() =>
    eventFeed.connected.subscribe((live) => {
      if (live) clearDead();
      else armDead();
    }),
  );
  // Scroll to the newest lap when the count grows (new lap captured, or first load).
  $effect(() => {
    if (laps.length > prevCount && lapsEl) {
      lapsEl.scrollTop = lapsEl.scrollHeight;
    }
    prevCount = laps.length;
  });

  onDestroy(clearDead);

  function formatTime(seconds: number | null): string {
    if (seconds == null || !Number.isFinite(seconds)) return "—";
    // Round to ms before splitting so e.g. 119.9996 -> "2:00.000", not "1:60.000".
    const totalMs = Math.round(seconds * 1000);
    const m = Math.floor(totalMs / 60000);
    const s = ((totalMs - m * 60000) / 1000).toFixed(3);
    return `${m}:${s.padStart(6, "0")}`;
  }

  // What a partial lap shows where its lap time would go. Its NUMBER is the reason
  // the row exists, so the row keeps everything else and only the time is replaced.
  const PARTIAL_WORD: Record<PartialKind, string> = {
    out: "out lap",
    in: "in lap",
    partial: "partial",
  };
  function timeText(lap: LapRow): string {
    return lap.partial ? PARTIAL_WORD[lap.partial] : formatTime(lap.lap_time);
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
      if (l.lap_time == null) continue;   // a partial lap is never the reference (#77)
      if (l.mapping) continue;            // nor a mapping weave (#154)
      if (l.s1 == null && l.s2 == null && l.s3 == null) continue;
      if (ref === null || l.lap_time < ref.lap_time!) ref = l;
    }
    return { s1: ref?.s1 ?? null, s2: ref?.s2 ?? null, s3: ref?.s3 ?? null };
  });

  // Per-sector delta vs the reference lap. cls drives color: pb (matches ref) /
  // up (slower, red) / down (faster than the reference lap, green).
  type SecInfo = { label: string; cls: string; delta: string; time: string };
  function sectorCell(label: string, v: number | null, b: number | null): SecInfo {
    const time = v == null ? "—" : v.toFixed(3);
    if (v == null || b == null) return { label, cls: "", delta: "—", time };
    if (v === b) return { label, cls: "pb", delta: "best", time };
    const d = v - b;
    return { label, cls: d > 0 ? "up" : "down", delta: (d < 0 ? "−" : "+") + Math.abs(d).toFixed(2), time };
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
  <div class="rail-title">
    <h2>This session</h2>
    {#if combo}
      <div class="combo">
        {#if combo.track}<span class="track">{combo.track}</span>{/if}
        {#if combo.car}<span class="car">{combo.car}</span>{/if}
      </div>
    {/if}
  </div>
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
      {#if group.phase === "race" && multiRace && group.raceIndex != null}
        <div class="phase-head">{raceHeader(group.laps[0].ts, group.raceIndex)}</div>
      {:else if group.phase}
        <div class="phase-head">{phaseLabel(group.phase)}</div>
      {/if}
      {#each group.laps as lap (lap.id)}
        {@const sectors = hasSectors(lap) ? sectorInfo(lap) : null}
        {@const open = expanded.has(lap.id)}
        <div class="lapwrap" class:open>
          <div
            class="lap"
            class:best={lap.id === bestId}
            class:partial={lap.partial != null}
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
                  Lap {lap.num} {#if lap.mapping}<span class="flag maptag" title="Captured while mapping: kept out of bests">map</span>{/if}
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
                <span class="t">{timeText(lap)}</span>
                <span class="flag">{lap.valid || lap.partial ? "" : "✗ inv"}</span>
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
                <span class="n">Lap {lap.num} {#if lap.mapping}<span class="flag maptag" title="Captured while mapping: kept out of bests">map</span>{/if}</span>
                <span class="t">{timeText(lap)}</span>
                <span class="flag">{lap.valid || lap.partial ? "" : "✗ inv"}</span>
              </button>
            {/if}
          </div>
          {#if sectors}
            <div class="lapdetail">
              <div class="secgrid">
                {#each sectors as s}
                  <div class="seccell">
                    <span class="sl">{s.label}</span>
                    <span class="st">{s.time}</span>
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
