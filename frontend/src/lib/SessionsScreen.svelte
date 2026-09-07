<script lang="ts">
  // Ported from docs/design/v1-ui-prototype.html #s-sessions. The prototype's
  // mock has game/cfg/cls/type fields the real /sessions payload doesn't --
  // this builds against the real SessionRow shape (session_id, track, car,
  // laps, best, started, last) and filters/groups by track + car only.
  import {
    getSessions,
    getSessionsSummary,
    getSessionChats,
    withRetry,
    type SessionRow,
    type SessionsSummary,
  } from "./api";
  import TrendChart from "./TrendChart.svelte";

  let { onselectSession }: { onselectSession?: (sessionId: string, chatId?: string) => void } = $props();

  // Open a session: resolve its most-recent linked chat (if any) and hand both up.
  async function selectSession(sessionId: string) {
    let chatId: string | undefined;
    try {
      const linked = await getSessionChats(sessionId);
      chatId = linked[0]?.chat_id;
    } catch {
      chatId = undefined;
    }
    onselectSession?.(sessionId, chatId);
  }

  const ALL = "All";
  const PAGE_SIZE = 30;

  // Backend owns filtering + pagination; the client only holds the current page
  // plus a summary (stats + trend) computed server-side over the full match set.
  let rows: SessionRow[] = $state([]);
  let total = $state(0);
  let tracks: string[] = $state([]);
  let cars: string[] = $state([]);
  let summary: SessionsSummary | null = $state(null);
  let loaded = $state(false);
  let selectedTrack = $state(ALL);
  let selectedCar = $state(ALL);
  let page = $state(1);

  const filterArgs = () => ({
    track: selectedTrack === ALL ? undefined : selectedTrack,
    car: selectedCar === ALL ? undefined : selectedCar,
  });

  function onFilterChange() {
    page = 1; // reset paging before the fetch effects react to the new filter
  }

  // Fetch the current page (reacts to filters + page). A token guards against
  // out-of-order responses when the user pages/filters quickly.
  let pageToken = 0;
  $effect(() => {
    const args = { ...filterArgs(), offset: (page - 1) * PAGE_SIZE, limit: PAGE_SIZE };
    const token = ++pageToken;
    (async () => {
      try {
        // Retry through the sidecar startup race — screens mount before bind.
        const res = await withRetry(() => getSessions(args));
        if (token !== pageToken) return;
        rows = res.rows;
        total = res.total;
        tracks = res.tracks;
        cars = res.cars;
      } catch {
        if (token !== pageToken) return;
        rows = [];
        total = 0;
      } finally {
        if (token === pageToken) loaded = true;
      }
    })();
  });

  // Fetch stats + trend for the filter (reacts to filters only, not page).
  let summaryToken = 0;
  $effect(() => {
    const args = filterArgs();
    const token = ++summaryToken;
    (async () => {
      try {
        const res = await getSessionsSummary(args);
        if (token === summaryToken) summary = res;
      } catch {
        if (token === summaryToken) summary = null;
      }
    })();
  });

  const pageCount = $derived(Math.max(1, Math.ceil(total / PAGE_SIZE)));

  function formatBest(seconds: number | null): string {
    if (seconds == null) return "—";
    const m = Math.floor(seconds / 60);
    const s = (seconds - m * 60).toFixed(3);
    return `${m}:${s.padStart(6, "0")}`;
  }

  function formatDate(epochSeconds: number): string {
    return new Date(epochSeconds * 1000).toLocaleDateString(undefined, {
      month: "short",
      day: "numeric",
    });
  }

  const personalBest = $derived(summary?.personal_best ?? null);
  const improvement = $derived(summary?.improvement ?? null);
  const consistency = $derived(summary?.consistency ?? null);
  const carCount = $derived(summary?.cars ?? 0);

  const chartSeries = $derived(
    (summary?.series ?? []).map((s) => ({
      name: s.car,
      points: s.points.map((p) => ({ x: p.x, label: formatDate(p.x), value: p.value })),
    })),
  );
</script>

<div class="page">
  <div class="page-h">
    <h1>Sessions</h1>
    <div class="filters">
      <select aria-label="Track" bind:value={selectedTrack} onchange={onFilterChange}>
        <option value={ALL}>All tracks</option>
        {#each tracks as t}
          <option value={t}>{t}</option>
        {/each}
      </select>
      <select aria-label="Car" bind:value={selectedCar} onchange={onFilterChange}>
        <option value={ALL}>All cars</option>
        {#each cars as c}
          <option value={c}>{c}</option>
        {/each}
      </select>
    </div>
  </div>

  {#if loaded && total === 0 && selectedTrack === ALL && selectedCar === ALL}
    <p class="empty">No sessions yet &mdash; drive some laps or import your history in Settings.</p>
  {:else}
    <div class="trend-wrap">
      <div class="chart-card">
        <h3>Best lap</h3>
        <div class="cap">lower is better</div>
        <TrendChart series={chartSeries} placeholder="Select a track to see the best-lap trend" />
      </div>

      <div class="stat-cards">
        <div class="scard">
          <div class="k">Personal best</div>
          <div class="v">{personalBest != null ? formatBest(personalBest) : "—"}</div>
        </div>
        <div class="scard">
          <div class="k">Improvement</div>
          <div class="v" class:good={improvement != null && improvement > 0}>
            {improvement != null ? `${improvement >= 0 ? "−" : "+"}${Math.abs(improvement).toFixed(3)}s` : "—"}
          </div>
        </div>
        <div class="scard">
          <div class="k">Sessions</div>
          <div class="v">{total}</div>
        </div>
        <div class="scard">
          <div class="k">{carCount > 1 ? "Cars" : "Consistency"}</div>
          <div class="v">
            {carCount > 1 ? carCount : consistency != null ? `±${consistency.toFixed(3)}s` : "—"}
          </div>
        </div>
      </div>

      <div class="sess-list-h">Recorded sessions <span>&middot; {total}</span></div>
      <div class="sess-list">
        {#if rows.length === 0}
          <p class="empty">No sessions match these filters.</p>
        {:else}
          {#each rows as s (s.session_id)}
            <div class="srow" role="button" tabindex="0" onclick={() => selectSession(s.session_id)} onkeydown={(e) => e.key === "Enter" && selectSession(s.session_id)}>
              <div class="d">{formatDate(s.started)}</div>
              <div class="tk">{s.track}<div class="ln"><small>{s.car_model ?? s.car}</small></div></div>
              <div class="lp">{s.laps} laps</div>
              <div class="bt">{formatBest(s.best)}</div>
            </div>
          {/each}
        {/if}
      </div>
      {#if pageCount > 1}
        <div class="pager">
          <button type="button" disabled={page <= 1} onclick={() => (page -= 1)}>Prev</button>
          <span>Page {page} of {pageCount}</span>
          <button type="button" disabled={page >= pageCount} onclick={() => (page += 1)}>Next</button>
        </div>
      {/if}
    </div>
  {/if}
</div>

<style>
  .page {
    height: 100%;
    overflow-y: auto;
    padding: 24px 30px;
  }
  .page-h {
    max-width: 1040px;
    margin: 0 auto 20px;
    display: flex;
    align-items: center;
    gap: 14px;
  }
  .page-h h1 {
    font-family: var(--display);
    font-size: 20px;
    font-weight: 700;
    margin: 0;
  }
  .filters {
    margin-left: auto;
    display: flex;
    gap: 8px;
    flex-wrap: wrap;
  }
  select {
    font-family: var(--body);
    font-size: 13px;
    color: var(--text);
    background: var(--surface);
    border: 1px solid var(--line);
    border-radius: 9px;
    padding: 8px 11px;
    cursor: pointer;
  }
  select:focus {
    outline: none;
    border-color: var(--accent);
  }
  .empty {
    max-width: 1040px;
    margin: 40px auto;
    text-align: center;
    color: var(--faint);
    font-family: var(--mono);
    font-size: 13px;
  }
  .trend-wrap {
    max-width: 1000px;
    margin: 0 auto;
  }
  .chart-card {
    background: var(--surface);
    border: 1px solid var(--line);
    border-radius: 14px;
    padding: 18px 20px;
    box-shadow: var(--shadow);
    margin-bottom: 16px;
  }
  .chart-card h3 {
    font-family: var(--display);
    font-size: 14px;
    font-weight: 600;
    margin: 0 0 2px;
  }
  .chart-card .cap {
    font-family: var(--mono);
    font-size: 11px;
    color: var(--faint);
    margin-bottom: 12px;
  }
  .stat-cards {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
    gap: 12px;
  }
  .scard {
    background: var(--surface);
    border: 1px solid var(--line);
    border-radius: 12px;
    padding: 14px 16px;
  }
  .scard .k {
    font-family: var(--display);
    font-size: 9px;
    text-transform: uppercase;
    letter-spacing: 0.1em;
    color: var(--faint);
  }
  .scard .v {
    font-family: var(--mono);
    font-weight: 600;
    font-size: 20px;
    margin-top: 4px;
  }
  .scard .v.good {
    color: var(--good);
  }
  .sess-list-h {
    font-family: var(--display);
    font-size: 11px;
    text-transform: uppercase;
    letter-spacing: 0.12em;
    color: var(--faint);
    margin: 26px 0 10px;
  }
  .sess-list-h span {
    font-family: var(--mono);
    color: var(--muted);
  }
  .sess-list {
    display: flex;
    flex-direction: column;
    gap: 8px;
  }
  .srow {
    display: grid;
    grid-template-columns: 80px 1fr auto auto;
    gap: 14px;
    align-items: center;
    background: var(--surface);
    border: 1px solid var(--line);
    border-radius: 10px;
    padding: 11px 15px;
    cursor: pointer;
  }
  .srow:hover {
    border-color: var(--accent);
  }
  .srow .d {
    font-family: var(--mono);
    font-size: 11px;
    color: var(--faint);
  }
  .srow .tk {
    font-weight: 600;
    font-size: 13px;
  }
  .srow .tk small {
    color: var(--muted);
    font-weight: 500;
  }
  .srow .tk .ln {
    margin-top: 2px;
  }
  .srow .lp {
    font-family: var(--mono);
    font-size: 12px;
    color: var(--muted);
  }
  .srow .bt {
    font-family: var(--mono);
    font-size: 13px;
    font-weight: 600;
    color: var(--accent);
  }
  .pager {
    display: flex;
    align-items: center;
    justify-content: center;
    gap: 14px;
    margin: 16px 0 4px;
    font-family: var(--mono);
    font-size: 12px;
    color: var(--muted);
  }
  .pager button {
    font-family: var(--display);
    font-size: 12px;
    font-weight: 600;
    color: var(--text);
    background: var(--surface);
    border: 1px solid var(--line);
    border-radius: 8px;
    padding: 6px 14px;
    cursor: pointer;
  }
  .pager button:disabled {
    opacity: 0.45;
    cursor: default;
  }
  .pager button:not(:disabled):hover {
    border-color: var(--accent);
  }
</style>
